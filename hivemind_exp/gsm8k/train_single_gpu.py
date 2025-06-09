import sys
import logging

# Needs to be before trl!
from hivemind_exp.runner.grpo_runner import GRPOArguments, GRPORunner

from trl import GRPOConfig, ModelConfig, TrlParser

from hivemind_exp.chain_utils import (
    ModalSwarmCoordinator,
    WalletSwarmCoordinator,
    setup_web3,
)
from hivemind_exp.gsm8k.generate_prompts import get_stage1_samples as gsm8k_stage1_samples
from hivemind_exp.dapo.generate_prompts import get_stage1_samples as dapo_stage1_samples
from hivemind_exp.debug_utils import print_system_info, TeeHandler, PrintCapture
from hivemind_exp.runner.gensyn.testnet_grpo_runner import (
    TestnetGRPOArguments,
    TestnetGRPORunner,
)


def main():
    # Setup logging
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    
    # Create and add the TeeHandler
    tee_handler = TeeHandler("logs/swarm.log", mode='w')
    tee_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(tee_handler)
    
    # Log system info and set up print capture
    root_logger.debug(print_system_info())
    sys.stdout = PrintCapture(root_logger)

    parser = TrlParser((ModelConfig, GRPOArguments, TestnetGRPOArguments, GRPOConfig))  # type: ignore
    model_args, grpo_args, testnet_args, training_args = parser.parse_args_and_config()
    training_args.logging_dir = "logs" 

    # Run main training loop.
    contract_address = testnet_args.contract_address
    if org_id := testnet_args.modal_org_id:
        assert contract_address, "Contract address must be set!"
        runner = TestnetGRPORunner(
            ModalSwarmCoordinator(setup_web3(), contract_address, org_id)
        )
    elif priv_key := testnet_args.wallet_private_key:
        assert contract_address, "Contract address must be set!"
        runner = TestnetGRPORunner(
            WalletSwarmCoordinator(setup_web3(), contract_address, priv_key)
        )
    else:
        runner = GRPORunner()

    game = grpo_args.game
    match game:
        case "gsm8k":
            runner.run(model_args, grpo_args, training_args, gsm8k_stage1_samples)
        case "dapo":
            runner.run(model_args, grpo_args, training_args, dapo_stage1_samples)
        case _:
            raise ValueError()


if __name__ == "__main__":
    main()
