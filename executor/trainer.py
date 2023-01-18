import os
import wandb
from pathlib import Path
from glob import glob
from torch import optim, nn, Tensor
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler
from torch.utils.data import DataLoader
from tqdm import tqdm
from abc import ABC
from torchmetrics import WordErrorRate


class Trainer(ABC):
    def __init__(
        self,
        max_epochs: int,
        experiment_path: str,
        wandb_conf: dict,
        optim_conf: dict,
        scheduler_conf: dict,
        text_process,
        device: str = 'cpu',
    ):
        super().__init__()
        self.device = device
        self.optim_conf = optim_conf
        self.scheduler_conf = scheduler_conf
        self.max_epochs = max_epochs
        self.wandb_conf = wandb_conf
        self.experiment_path = experiment_path

        if os.path.exists(experiment_path):
            os.mkdir(experiment_path)

        if wandb_conf.is_log:
            wandb.init(**wandb_conf.config)

    def get_optimizer_and_scheduler(self, model: nn.Module, dataloader: DataLoader):
        optimizer = getattr(optim, self.optim_conf.optim_name)(
            model.paramters(), **self.optim_conf
        )
        if self.scheduler_conf.sched_name == "OneCycleLR":
            # for training only
            self.scheduler_conf.update({"total_steps": len(dataloader) * self.max_epochs})
        scheduler = getattr(optim.lr_scheduler, self.scheduler_conf.sched_name)(
            optimizer, **self.scheduler_conf
        )

        if self.optim_ckpt:
            optimizer.load_state_dict(self.optim_ckpt.get("optimizer_state_dict"))
            scheduler.load_state_dict(self.optim_ckpt.get("scheduler_state_dict"))

        return optimizer, scheduler

    def save_ckpt(
        self,
        model: nn.Module,
        optimizer: Optimizer,
        scheduler: _LRScheduler,
        epoch: int = -1,
        step: int = -1,
    ) -> None:
        trainer = dict(
            optim=dict(
                optimizer_state_dict=optimizer.state_dict(),
                scheduler_state_dict=scheduler.state_dict(),
            ),
            hyperparams=self.__dict__,
        )

        model = dict(model_state_dict=model.state_dict(), hyperparams=model.__dict__)

        version = len(glob(str(Path(self.experiment_path) / "version_*")))
        version_path = os.path.join(self.experiment_path, f"version_{version}")
        os.mkdir(version_path)

        trainer_name = f"{self.__class__.__name__}.epoch={epoch}.step={step}.pt"
        model_name = f"{model.__class__.__name__}.epoch={epoch}.step={step}.pt"

        trainer_path = os.path.join(version_path, trainer_name)
        model_path = os.path.join(version_path, model_name)

        torch.save(trainer, trainer_path)
        torch.save(model, model_path)

    def load_from_ckpt(self, ckpt_path: dict) -> None:
        trainer_ckpt_path = ckpt_path.get("trainer")

        if trainer_ckpt_path:
            trainer = torch.load(trainer_ckpt_path)
            for k, v in trainer.get("hyperparams"):
                setattr(self, k, v)

        print("<Restore Trainer checkpoint successfully>")

    def train(self, model: nn.Module, dataloader: DataLoader):
        optimizer, scheduler = self.get_optimizer_and_scheduler(model, dataloader)

        for epoch in range(1, self.max_epochs + 1):
            self.train_epoch(model, dataloader, optimizer, scheduler, epoch)
            self.test_epoch(model, dataloader, epoch, "valid")

            if self.scheduler_conf.interval == "epoch":
                scheduler.step()

    def test(self, model: nn.Module, dataloader: DataLoader):
        self.test_epoch(model, dataloader, 0, "test")

    def predict(self, model: nn.Module, dataloader: DataLoader, outcome_path: str):
        self.test_epoch(model, dataloader, 0, outcome_path)

    def train_epoch(
        self,
        model: nn.Module,
        dataloader: DataLoader,
        optimizer: Optimizer,
        scheduler: _LRScheduler,
        epoch: int,
    ):
        pass

    def test_epoch(
        self,
        model: nn.Module,
        dataloader: DataLoader,
        epoch: int,
        task: str = "test",
        outcome_name: str = None,
    ):
        pass


class TeacherTrainer(Trainer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def train_epoch(
        self,
        model: nn.Module,
        dataloader: DataLoader,
        optimizer: Optimizer,
        scheduler: _LRScheduler,
        epoch: int,
    ):
        size = len(dataloader)
        pbar = tqdm(dataloader, total=size)

        for batch_idx, batch in enumerate(tqdm, start=1):
            feat, feat_len, target, target_len = list(
                map(lambda x: x.to(self.device), batch)
            )

            optimizer.zero_grad()

            # for training only
            out, out_len, loss = model(feat, feat_len, target, target_len)

            loss.backward()

            optimizer.step()

            if self.scheduler_conf.interval == "step":
                scheduler.step()

            if self.wandb_conf.is_log:
                wandb.log({"train/loss": loss.item()})

                sched_name = scheduler.__class__.__name__
                last_lr = scheduler.get_last_lr()[0]
                wandb.log({f"lr-{sched_name}": last_lr})

            pbar.set_description(f"[Epoch: {epoch}] Loss: {loss.item():.2f}")

            self.save_ckpt(model, optimizer, scheduler, epoch, epoch * batch_idx)

    def test_epoch(
        self,
        model: nn.Module,
        dataloader: DataLoader,
        epoch: int,
        task: str = "test",
        outcome_name: str = None,
    ):
        size = len(dataloader)
        pbar = tqdm(dataloader, total=size)
        cal_wer = WordErrorRate()

        outcome_path = os.path.join(self.experiment_path, outcome_name)

        with open(outcome_path, "a") as f:
            f.write("=" * 10 + f"{task} | Epoch: {epoch}" + "=" * 10)
            f.write("\n")

        with torch.inference_mode():
            for batch_idx, batch in enumerate(tqdm, start=1):
                feat, feat_len, target, target_len = list(
                    map(lambda x: x.to(self.device), batch)
                )

                # for training only
                out, out_len, loss = model(
                    feat, feat_len, target, target_len, predict=True
                )

                predict = model.recognize(inputs, input_lengths)
                actual = list(map(self.text_process.int2text, targets))
                list_wer = [
                    cal_wer(hypot, truth).item()
                    for hypot, truth in zip(predict, actual)
                ]
                mean_wer = cal_wer(predict, actual).item()

                with open(outcome_path, "a") as f:
                    for pred, act, wer in zip(predict, actual, list_wer):
                        f.write(f"PER    : {wer}\n")
                        f.write(f"Actual : {act}\n")
                        f.write(f"Predict: {pred}\n")
                        f.write("=" * 20 + "\n")

                if self.wandb_conf.is_log:
                    wandb.log({f"{task}/loss": loss.item()})
                    wandb.log({f"{task}/wer": mean_wer})

                pbar.set_description(
                    f"[Epoch: {epoch}] Loss: {loss.item():.2f} | WER: {mean_wer:.2f}%"
                )


class StudentTrainer(Trainer):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def train_epoch(
        self,
        teacher_model: nn.Module,
        student_model: nn.Module,
        dataloader: DataLoader,
        optimizer: Optimizer,
        scheduler: _LRScheduler,
        epoch: int,
    ):
        size = len(dataloader)
        pbar = tqdm(dataloader, total=size)

        for batch_idx, batch in enumerate(tqdm, start=1):
            feat, feat_len, trans = batch
            feat, feat_len = feat.to(self.device), feat_len.to(self.device)

            # teacher generate pseudo-label for student learning
            predicted = teacher_model.recognize(inputs, input_lengths)

            # replace the origin transcript of timit dataset
            for origin_trans, idx in trans:
                predicted[idx] = origin_trans

            for i in range(len(predicted)):
                if type(predicted[i]) == str:
                    predicted[i] = self.text_process.tokenize(predicted[i])

            predicted = [self.text_process.text2int(s) for s in predicted]
            
            target_len = torch.IntTensor([s.size(0) for s in predicted]).to(self.device)
            target = pad_sequence(predicted, batch_first=True).to(self.device, torch.int)

            optimizer.zero_grad()

            # for training only
            out, out_len, loss = student_model(feat, feat_len, target, target_len)

            loss.backward()

            optimizer.step()

            if self.scheduler_conf.interval == "step":
                scheduler.step()

            if self.wandb_conf.is_log:
                wandb.log({"train/loss": loss.item()})

                sched_name = scheduler.__class__.__name__
                last_lr = scheduler.get_last_lr()[0]
                wandb.log({f"lr-{sched_name}": last_lr})

            pbar.set_description(f"[Epoch: {epoch}] Loss: {loss.item():.2f}")

            self.save_ckpt(student_model, optimizer, scheduler, epoch, epoch * batch_idx)

    def test_epoch(
        self,
        student_model: nn.Module,
        dataloader: DataLoader,
        epoch: int,
        outcome_name: str = None,
    ):
        size = len(dataloader)
        pbar = tqdm(dataloader, total=size)
        cal_wer = WordErrorRate()

        outcome_path = os.path.join(self.experiment_path, outcome_name)

        with open(outcome_path, "a") as f:
            f.write("=" * 10 + f"{task} | Epoch: {epoch}" + "=" * 10)
            f.write("\n")

        with torch.inference_mode():
            for batch_idx, batch in enumerate(tqdm, start=1):
                feat, feat_len, target, target_len = list(
                    map(lambda x: x.to(self.device), batch)
                )

                # for training only
                out, out_len, loss = student_model(
                    feat, feat_len, target, target_len, predict=True
                )

                predict = student_model.recognize(inputs, input_lengths)
                actual = list(map(self.text_process.int2text, targets))
                list_wer = [
                    cal_wer(hypot, truth).item()
                    for hypot, truth in zip(predict, actual)
                ]
                mean_wer = cal_wer(predict, actual).item()

                with open(outcome_path, "a") as f:
                    for pred, act, wer in zip(predict, actual, list_wer):
                        f.write(f"PER    : {wer}\n")
                        f.write(f"Actual : {act}\n")
                        f.write(f"Predict: {pred}\n")
                        f.write("=" * 20 + "\n")

                if self.wandb_conf.is_log:
                    wandb.log({f"{task}/loss": loss.item()})
                    wandb.log({f"{task}/wer": mean_wer})

                pbar.set_description(
                    f"[Epoch: {epoch}] Loss: {loss.item():.2f} | WER: {mean_wer:.2f}%"
                )
