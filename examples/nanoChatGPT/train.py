"""
Train the transformer model. Configurable via config/train.yaml, but any argument can
also be overridden at the command line.

To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False
"""

import os
import time
from pathlib import Path

import evaluate
import torch
import numpy as np

from data.openai_summarize_tldr import get_prompt_dataloaders
from models.transformer import init_transformer
from utils import create_lr_scheduler, load_and_update_config, setup
from transformers import AutoTokenizer

HERE = Path(__file__).parent


def init_scaler(config):
    # initialize a GradScaler. If enabled=False scaler is a no-op
    return torch.cuda.amp.GradScaler(enabled=(config["dtype"] == "float16"))


def compute_rouge(batch_td, tokenizer, rouge_fn):
    labels_ids = batch_td.transformer_data.labels[:, 1:]
    pred_ids = batch_td.transformer_data.logits.argmax(dim=-1)[:, :-1]
    pred_str = tokenizer.batch_decode(pred_ids, skip_special_tokens=True)
    label_str = tokenizer.batch_decode(labels_ids, skip_special_tokens=True)
    result = rouge_fn.compute(predictions=pred_str, references=label_str)
    return result


def create_loss_estimator(config, ctx):
    # helps estimate an arbitrarily accurate loss over either split using many batches
    rouge_fn = evaluate.load("rouge")
    tokenizer = AutoTokenizer.from_pretrained("gpt2")
    tokenizer.pad_token = tokenizer.eos_token
    
    @torch.no_grad()
    def estimate_loss(model, dataloader, return_rouge=False):
        model.eval()
        losses = torch.zeros(config["eval_iters"])
        if return_rouge:
            rouge1 = torch.zeros(config["eval_iters"])
        for k in range(config["eval_iters"]):
            batch = next(dataloader)
            with ctx:
                model(batch)
            losses[k] = batch.loss.item()
            if return_rouge:
                rouge = compute_rouge(batch, tokenizer, rouge_fn)
                rouge1[k] = rouge["rouge1"]
        model.train()
        if return_rouge:
            return losses.mean(), rouge1.mean()
        return losses.mean()

    return estimate_loss


def main():
    config = load_and_update_config("config/train.yaml")
    ctx = setup(config)

    # ######## INIT MODELS ########
    model = init_transformer(config)

    # ######## INIT TRAINING FUNCTIONS ########
    scaler = init_scaler(config)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config["learning_rate"],
        weight_decay=config["weight_decay"],
        betas=(config["beta1"], config["beta2"]),
    )

    lr_scheduler = create_lr_scheduler(config)

    estimate_loss = create_loss_estimator(config, ctx)

    train_loader, val_loader = get_prompt_dataloaders(config)

    # these will already have been set if resuming from previous checkpoint
    iter_num = config.setdefault("iter_num", 0)
    best_val_loss = config.setdefault("best_val_loss", 1e9)

    train_losses = []
    val_losses = []
    train_rouges = []
    val_rouges = []
    # ######## TRAINING LOOP ########
    t0 = time.time()
    next_batch = next(train_loader)  # fetch the very first batch
    for it in range(iter_num, config["max_iters"]):
        # get and update the learning rate
        lr = lr_scheduler(it)
        for param_group in optimizer.param_groups:
            param_group["lr"] = lr

        # FIXME: do we need gradient accumulation steps? why we are doing this only in train?

        # forward backward update, with optional gradient accumulation to simulate larger batch size
        # and using the GradScaler if data type is float16
        for _ in range(config["gradient_accumulation_steps"]):
            batch = next_batch
            with ctx:
                model(batch)
            # immediately async prefetch next batch while model is doing the forward pass on the GPU
            next_batch = next(train_loader)
            # backward pass, with gradient scaling if training in fp16
            scaler.scale(batch.loss).backward()
        # clip the gradient
        if config["grad_clip"] != 0.0:
            scaler.unscale_(optimizer)
            torch.nn.utils.clip_grad_norm_(model.parameters(), config["grad_clip"])
        # step the optimizer and scaler if training in fp16
        scaler.step(optimizer)
        scaler.update()
        # flush the gradients as soon as we can, no need for this memory anymore
        optimizer.zero_grad(set_to_none=True)

        # ########### EVALUATE MODEL AND CHECKPOINT ###############
        # timing and logging
        t1 = time.time()
        dt = t1 - t0
        t0 = t1
        if it % config["eval_interval"] == 0:
            # evaluate the loss on train/val sets and write checkpoints
            if False:  # return_rouge:
                val_loss, val_rouge = estimate_loss(model, val_loader, return_rouge=True)
                train_loss, train_rouge = estimate_loss(model, train_loader, return_rouge=True)
                print(
                    f"Evaluation: iter {it}: train loss {train_loss:.4f}, val loss {val_loss:.4f}, "
                    f"train rouge {train_rouge:.4f}, val rouge {val_rouge:.4f}"
                )
                train_rouges.append(train_rouge)
                val_rouges.append(val_rouge)
            else:
                val_loss = estimate_loss(model, val_loader)
                train_loss = estimate_loss(model, train_loader)
                print(
                    f"Evaluation: iter {it}: train loss {train_loss:.4f}, val loss {val_loss:.4f}"
                )
            train_losses.append(train_loss)
            val_losses.append(val_loss)

            if val_loss < best_val_loss or config["always_save_checkpoint"]:
                best_val_loss = val_loss
                if it > 0:
                    checkpoint = {
                        "optimizer": optimizer.state_dict(),
                        "iter_num": it,
                        "best_val_loss": best_val_loss,
                        "config": config,
                    }
                    print(f"saving checkpoint to {config['out_dir']}")
                    model.module.save_pretrained(config["out_dir"])
                    torch.save(
                        checkpoint, os.path.join(config["out_dir"], "ckpt_status.pt")
                    )

        elif it % config["log_interval"] == 0:
            # loss as float. note: this is a CPU-GPU sync point
            lossf = batch.loss.item()
            train_losses.append(lossf)
            print(f"iter {it}: train loss {lossf:.4f}, time {dt*1000:.2f}ms")

    import matplotlib.pyplot as plt

    f, ax = plt.subplots(figsize=(8, 6))
    plt.title('Supervised Fine Tuning: Loss')
    ax.plot(np.arange(0, config["max_iters"], config["log_interval"]), train_losses, label="train loss")
    ax.plot(np.arange(0, config["max_iters"], config["eval_interval"]), val_losses, label="valid loss")
    ax.legend()
    f.savefig("figures/train_curve.png", dpi=150)

    if False:
        f, ax = plt.subplots(figsize=(8, 6))
        plt.title('Supervised Fine Tuning: Rouge')
        ax.plot(np.arange(0, config["max_iters"], config["eval_interval"]), train_rouges, label="train rouge")
        ax.plot(np.arange(0, config["max_iters"], config["eval_interval"]), val_rouges, label="valid rouge")
        ax.legend()
        f.savefig("figures/train_curve_rouge.png", dpi=150)

if __name__ == "__main__":
    main()
