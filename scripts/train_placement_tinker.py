"""LoRA SFT for the speculative placement policy on Thinking Machines Tinker."""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

DEFAULT_MODEL = "Qwen/Qwen3.5-4B"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("data", help="Conversation JSONL from export_placement_sft.py")
    parser.add_argument("--log-dir", default="artifacts/tinker-placement-sft")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--test-size", type=int, default=64)
    return parser


def _validate(args: argparse.Namespace) -> None:
    if not os.environ.get("TINKER_API_KEY"):
        raise SystemExit("TINKER_API_KEY is required; create it in the Tinker console")
    if not Path(args.data).is_file():
        raise SystemExit(f"training data not found: {args.data}")
    if args.batch_size <= 0 or args.epochs <= 0:
        raise SystemExit("batch size and epochs must be positive")


def main() -> int:
    args = _parser().parse_args()
    _validate(args)

    from tinker_cookbook import model_info
    from tinker_cookbook.renderers import TrainOnWhat
    from tinker_cookbook.supervised import train
    from tinker_cookbook.supervised.data import FromConversationFileBuilder
    from tinker_cookbook.supervised.types import ChatDatasetBuilderCommonConfig

    renderer = model_info.get_recommended_renderer_name(args.model)
    common = ChatDatasetBuilderCommonConfig(
        model_name_for_tokenizer=args.model,
        renderer_name=renderer,
        max_length=4096,
        batch_size=args.batch_size,
        train_on_what=TrainOnWhat.ALL_ASSISTANT_MESSAGES,
    )
    dataset = FromConversationFileBuilder(
        common_config=common,
        file_path=str(Path(args.data)),
        test_size=args.test_size,
        shuffle_seed=0,
    )
    config = train.Config(
        log_path=args.log_dir,
        model_name=args.model,
        recipe_name="silkscreen_placement_sft",
        renderer_name=renderer,
        dataset_builder=dataset,
        learning_rate=2e-4,
        lr_schedule="linear",
        num_epochs=args.epochs,
        lora_rank=32,
        eval_every=0,
        save_every=0,
        max_steps=args.max_steps,
        submit_ahead=1,
    )
    asyncio.run(train.main(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
