from typing import List, Optional, Tuple

import pandas as pd
import pytorch_lightning as pl
import torch
import torch.nn.functional as F
import lpmm
import wandb
from peft import LoraConfig, get_peft_model
from torch.optim import SGD, Adam, AdamW
from torch.optim.lr_scheduler import LambdaLR
from torchmetrics import MetricCollection
from torchmetrics.classification.accuracy import Accuracy
from torchmetrics.classification.stat_scores import StatScores
from transformers import AutoConfig, AutoModelForImageClassification, BitsAndBytesConfig
from transformers.optimization import get_cosine_schedule_with_warmup

from src.loss import SoftTargetCrossEntropy
from src.mixup import Mixup

MODEL_DICT = {
    "vit-b16-224-in21k": "google/vit-base-patch16-224-in21k",
    "vit-b32-224-in21k": "google/vit-base-patch32-224-in21k",
    "vit-l32-224-in21k": "google/vit-large-patch32-224-in21k",
    "vit-l15-224-in21k": "google/vit-large-patch16-224-in21k",
    "vit-h14-224-in21k": "google/vit-huge-patch14-224-in21k",
    "vit-b16-224": "google/vit-base-patch16-224",
    "vit-l16-224": "google/vit-large-patch16-224",
    "vit-b16-384": "google/vit-base-patch16-384",
    "vit-b32-384": "google/vit-base-patch32-384",
    "vit-l16-384": "google/vit-large-patch16-384",
    "vit-l32-384": "google/vit-large-patch32-384",
    "vit-b16-224-dino": "facebook/dino-vitb16",
    "vit-b8-224-dino": "facebook/dino-vitb8",
    "vit-s16-224-dino": "facebook/dino-vits16",
    "vit-s8-224-dino": "facebook/dino-vits8",
    "beit-b16-224-in21k": "microsoft/beit-base-patch16-224-pt22k-ft22k",
    "beit-l16-224-in21k": "microsoft/beit-large-patch16-224-pt22k-ft22k",
}


class ClassificationModel(pl.LightningModule):
    def __init__(
        self,
        model_name: str = "vit-b16-224-in21k",
        optimizer: str = "sgd",
        lr: float = 1e-2,
        betas: Tuple[float, float] = (0.9, 0.999),
        momentum: float = 0.9,
        weight_decay: float = 0.0,
        scheduler: str = "cosine",
        warmup_steps: int = 0,
        n_classes: int = 10,
        mixup_alpha: float = 0.0,
        cutmix_alpha: float = 0.0,
        mix_prob: float = 1.0,
        label_smoothing: float = 0.0,
        image_size: int = 224,
        weights: Optional[str] = None,
        training_mode: str = "full",
        lora_r: int = 16,
        lora_alpha: int = 16,
        lora_target_modules: List[str] = ["query", "value"],
        lora_dropout: float = 0.0,
        lora_bias: str = "none",
        from_scratch: bool = False,
        batch_size: int = 32,
        filename: str = "none",
        open_gact: bool = False,
        gact_level: str = "L0",
        use_4bit: bool = False,
    ):
        """Classification Model

        Args:
            model_name: Name of model checkpoint. List found in src/model.py
            optimizer: Name of optimizer. One of [adam, adamw, sgd]
            lr: Learning rate
            betas: Adam betas parameters
            momentum: SGD momentum parameter
            weight_decay: Optimizer weight decay
            scheduler: Name of learning rate scheduler. One of [cosine, none]
            warmup_steps: Number of warmup steps
            n_classes: Number of target class
            mixup_alpha: Mixup alpha value
            cutmix_alpha: Cutmix alpha value
            mix_prob: Probability of applying mixup or cutmix (applies when mixup_alpha and/or
                cutmix_alpha are >0)
            label_smoothing: Amount of label smoothing
            image_size: Size of input images
            weights: Path of checkpoint to load weights from (e.g when resuming after linear probing)
            training_mode: Fine-tuning mode. One of ["full", "linear", "lora"]
            lora_r: Dimension of LoRA update matrices
            lora_alpha: LoRA scaling factor
            lora_target_modules: Names of the modules to apply LoRA to
            lora_dropout: Dropout probability for LoRA layers
            lora_bias: Whether to train biases during LoRA. One of ['none', 'all' or 'lora_only']
            from_scratch: Initialize network with random weights instead of a pretrained checkpoint
            batch_size: Batch size
            filename: Name of the checkpoint file & wandb run name
            open_gact: Whether to use GACT
            gact_level: GACT level. One of ['L0', 'L1', 'L1.2', 'L2.2']
        """
        super().__init__()
        self.save_hyperparameters()
        self.model_name = model_name
        self.optimizer = optimizer
        self.lr = lr
        self.betas = betas
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.scheduler = scheduler
        self.warmup_steps = warmup_steps
        self.n_classes = n_classes
        self.mixup_alpha = mixup_alpha
        self.cutmix_alpha = cutmix_alpha
        self.mix_prob = mix_prob
        self.label_smoothing = label_smoothing
        self.image_size = image_size
        self.weights = weights
        self.training_mode = training_mode
        self.lora_r = lora_r
        self.lora_alpha = lora_alpha
        self.lora_target_modules = lora_target_modules
        self.lora_dropout = lora_dropout
        self.lora_bias = lora_bias
        self.from_scratch = from_scratch
        self.batch_size = batch_size
        self.filename = filename
        self.open_gact = open_gact
        self.gact_level = gact_level
        self.use_4bit = use_4bit

        # Initialize network
        try:
            model_path = MODEL_DICT[self.model_name]
        except:
            raise ValueError(
                f"{model_name} is not an available model. Should be one of {[k for k in MODEL_DICT.keys()]}"
            )

        if self.from_scratch:
            # Initialize with random weights
            config = AutoConfig.from_pretrained(model_path)
            config.image_size = self.image_size
            self.net = AutoModelForImageClassification.from_config(config)
            self.net.classifier = torch.nn.Linear(config.hidden_size, self.n_classes)
        else:
            # Initialize with pretrained weights
            if self.use_4bit:
                assert self.training_mode != "full", "4-bit quantization can not work with full fine-tuning mode"
                self.net = AutoModelForImageClassification.from_pretrained(
                    model_path,
                    num_labels=self.n_classes,
                    ignore_mismatched_sizes=True,
                    image_size=self.image_size,
                    quantization_config=BitsAndBytesConfig(
                        load_in_4bit=True,
                        bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True, bnb_4bit_quant_type="nf4",
                        llm_int8_skip_modules=["classifier"],
                    ),
                )
            else:
                self.net = AutoModelForImageClassification.from_pretrained(
                    model_path,
                    num_labels=self.n_classes,
                    ignore_mismatched_sizes=True,
                    image_size=self.image_size,
                )

        print(self.net)

        # Load checkpoint weights
        if self.weights:
            print(f"Loaded weights from {self.weights}")
            ckpt = torch.load(self.weights)["state_dict"]

            # Remove prefix from key names
            new_state_dict = {}
            for k, v in ckpt.items():
                if k.startswith("net"):
                    k = k.replace("net" + ".", "")
                    new_state_dict[k] = v

            self.net.load_state_dict(new_state_dict, strict=True)

        # Prepare model depending on fine-tuning mode
        if self.training_mode == "linear":
            # Freeze transformer layers and keep classifier unfrozen
            for name, param in self.net.named_parameters():
                if "classifier" not in name:
                    param.requires_grad = False
        elif self.training_mode == "lora":
            # Wrap in LoRA model
            config = LoraConfig(
                r=self.lora_r,
                lora_alpha=self.lora_alpha,
                target_modules=self.lora_target_modules,
                lora_dropout=self.lora_dropout,
                bias=self.lora_bias,
                modules_to_save=["classifier"],
            )
            self.net = get_peft_model(self.net, config)
        elif self.training_mode == "full":
            pass  # Keep all layers unfrozen
        else:
            raise ValueError(
                f"{self.training_mode} is not an available fine-tuning mode. Should be one of ['full', 'linear', 'lora']"
            )

        # Define metrics
        self.train_metrics = MetricCollection(
            {
                "acc": Accuracy(num_classes=self.n_classes, task="multiclass", top_k=1),
                "acc_top5": Accuracy(
                    num_classes=self.n_classes,
                    task="multiclass",
                    top_k=min(5, self.n_classes),
                ),
            }
        )
        self.val_metrics = MetricCollection(
            {
                "acc": Accuracy(num_classes=self.n_classes, task="multiclass", top_k=1),
                "acc_top5": Accuracy(
                    num_classes=self.n_classes,
                    task="multiclass",
                    top_k=min(5, self.n_classes),
                ),
            }
        )
        self.test_metrics = MetricCollection(
            {
                "acc": Accuracy(num_classes=self.n_classes, task="multiclass", top_k=1),
                "acc_top5": Accuracy(
                    num_classes=self.n_classes,
                    task="multiclass",
                    top_k=min(5, self.n_classes),
                ),
                "stats": StatScores(
                    task="multiclass", average=None, num_classes=self.n_classes
                ),
            }
        )

        # Define loss
        self.loss_fn = SoftTargetCrossEntropy()

        # Define regularizers
        self.mixup = Mixup(
            mixup_alpha=self.mixup_alpha,
            cutmix_alpha=self.cutmix_alpha,
            prob=self.mix_prob,
            label_smoothing=self.label_smoothing,
            num_classes=self.n_classes,
        )

        self.test_metric_outputs = []
        
        # Define wandb logger
        wandb.init(
            project=f"{self.model_name}_{self.training_mode}",
            name=f"lr_{self.lr}_bs_{self.batch_size}_optim_{self.optimizer}_scheduler_{self.scheduler}_gact_{self.gact_level}_4bit_{self.use_4bit}",
        )
        self.train_step = 0
        self.val_step = 0
        wandb.define_metric("train_step")
        wandb.define_metric("val_step")
        # set all other train/ metrics to use this step
        wandb.define_metric("train_*", step_metric="train_step")
        wandb.define_metric("val_*", step_metric="val_step")

        # Enable GACT
        if self.open_gact:
            import gact
            from gact.controller import Controller
            gact.set_optimization_level(self.gact_level) # set optmization level, more config info can be seen in gact/conf.py
            self.controller = Controller(self.net)
            self.controller.install_hook()
            print("GACT is enabled")

    def forward(self, x):
        return self.net(pixel_values=x).logits
    
    def gact_backward(self):
        optimizer_tmp = self.optimizers()
        partial_pred = self(self.partial_x)
        loss = self.loss_fn(partial_pred, self.partial_y)
        self.optimizer_zero_grad(self.current_epoch, 0, optimizer_tmp)
        self.backward(loss)

    def shared_step(self, batch, mode="train"):
        x, y = batch
        self.train_step += 1 if mode == "train" else 0
        self.val_step += 1 if mode == "val" else 0

        if mode == "train":
            # Only converts targets to one-hot if no label smoothing, mixup or cutmix is set
            x, y = self.mixup(x, y)
        else:
            y = F.one_hot(y, num_classes=self.n_classes).float()

        #! iterate the last step of gact
        if self.open_gact and mode == "train" and self.train_step != 1:
            self.controller.iterate(self.gact_backward)

        # Pass through network
        pred = self(x)
        loss = self.loss_fn(pred, y)

        # only for GACT
        if self.open_gact:
            self.partial_x = x[:8]
            self.partial_y = y[:8]
        
        # optimizer step
        # iterate the controller

        # Get accuracy
        metrics = getattr(self, f"{mode}_metrics")(pred, y.argmax(1))

        # Log
        self.log(f"{mode}_loss", loss, on_epoch=True)
        wandb.log({f"{mode}_loss": loss, f"{mode}_step": self.train_step if mode == "train" else self.val_step})
        
        for k, v in metrics.items():
            if len(v.size()) == 0:
                self.log(f"{mode}_{k.lower()}", v, on_epoch=True)
                wandb.log({f"{mode}_{k.lower()}": v, f"{mode}_step": self.train_step if mode == "train" else self.val_step})

        if mode == "test":
            self.test_metric_outputs.append(metrics["stats"])

        return loss

    def training_step(self, batch, _):
        self.log("lr", self.trainer.optimizers[0].param_groups[0]["lr"], prog_bar=True)
        return self.shared_step(batch, "train")

    def validation_step(self, batch, _):
        return self.shared_step(batch, "val")

    def test_step(self, batch, _):
        return self.shared_step(batch, "test")

    def on_test_epoch_end(self):
        """Save per-class accuracies to csv"""
        # Aggregate all batch stats
        combined_stats = torch.sum(
            torch.stack(self.test_metric_outputs, dim=-1), dim=-1
        )

        # Calculate accuracy per class
        per_class_acc = []
        for tp, _, _, _, sup in combined_stats:
            acc = tp / sup
            per_class_acc.append((acc.item(), sup.item()))

        # Save to csv
        df = pd.DataFrame(per_class_acc, columns=["acc", "n"])
        df.to_csv("per-class-acc-test.csv")
        print("Saved per-class results in per-class-acc-test.csv")

    def configure_optimizers(self):
        # Initialize optimizer
        if self.optimizer == "adam":
            optimizer = Adam(
                self.net.parameters(),
                lr=self.lr,
                betas=self.betas,
                weight_decay=self.weight_decay,
            )
        elif self.optimizer == "adamw":
            optimizer = AdamW(
                self.net.parameters(),
                lr=self.lr,
                betas=self.betas,
                weight_decay=self.weight_decay,
            )
        elif self.optimizer == "sgd":
            optimizer = SGD(
                self.net.parameters(),
                lr=self.lr,
                momentum=self.momentum,
                weight_decay=self.weight_decay,
            )
        elif self.optimizer == "adamw4bit":
            optimizer = lpmm.optim.AdamW(
                self.net.parameters(),
                lr=self.lr,
                betas=self.betas,
                weight_decay=self.weight_decay,
            )
        else:
            raise ValueError(
                f"{self.optimizer} is not an available optimizer. Should be one of ['adam', 'adamw', 'sgd']"
            )

        # Initialize learning rate scheduler
        if self.scheduler == "cosine":
            scheduler = get_cosine_schedule_with_warmup(
                optimizer,
                num_training_steps=int(self.trainer.estimated_stepping_batches),
                num_warmup_steps=self.warmup_steps,
            )
        elif self.scheduler == "none":
            scheduler = LambdaLR(optimizer, lambda _: 1)
        else:
            raise ValueError(
                f"{self.scheduler} is not an available optimizer. Should be one of ['cosine', 'none']"
            )

        return {
            "optimizer": optimizer,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
            },
        }
