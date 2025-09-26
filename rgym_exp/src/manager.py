import os
import time
import random
from collections import defaultdict

from genrl.blockchain import SwarmCoordinator
from genrl.communication import Communication
from genrl.communication.hivemind.hivemind_backend import HivemindBackend
from genrl.data import DataManager
from genrl.game import BaseGameManager
from genrl.game.game_manager import DefaultGameManagerMixin
from genrl.logging_utils.global_defs import get_logger
from genrl.logging_utils.system_utils import get_system_info
from genrl.rewards import RewardManager
from genrl.roles import RoleManager
from genrl.state import GameState
from genrl.trainer import TrainerModule
from huggingface_hub import login, whoami

from rgym_exp.src.utils.name_utils import get_name_from_peer_id
from rgym_exp.src.prg_module import PRGModule


class SwarmGameManager(BaseGameManager, DefaultGameManagerMixin):
    """GameManager that orchestrates a game using a SwarmCoordinator."""

    def __init__(
        self,
        coordinator: SwarmCoordinator,
        max_stage: int,
        max_round: int,
        game_state: GameState,
        reward_manager: RewardManager,
        trainer: TrainerModule,
        data_manager: DataManager,
        communication: Communication,
        role_manager: RoleManager | None = None,
        run_mode: str = "train",
        log_dir: str = "logs",
        hf_token: str | None = None,
        hf_push_frequency: int = 20,
        **kwargs,
    ):

        super().__init__(
            max_stage=max_stage,
            max_round=max_round,
            game_state=game_state,
            reward_manager=reward_manager,
            trainer=trainer,
            data_manager=data_manager,
            communication=communication,
            role_manager=role_manager,
            run_mode=run_mode,
        )

        assert isinstance(self.communication, HivemindBackend)
        self.train_timeout = 60 * 60 * 24 * 31  # 1 month

        # Logging Setup
        self.peer_id = self.communication.get_id()
        self.state.peer_id = self.peer_id
        self.animal_name = get_name_from_peer_id(self.peer_id, True)

        # Register peer_id and get current round from the chain
        self.coordinator = coordinator
        self.coordinator.register_peer(self.peer_id)
        round, _ = self.coordinator.get_round_and_stage()
        self.state.round = round

        self.communication.step_ = (
            self.state.round
        )  # initialize communication module to contract's round

        # enable push to HF if token was provided
        self.hf_token = hf_token
        if self.hf_token not in [None, "None"]:
            self._configure_hf_hub(hf_push_frequency)

        get_logger().info(
            f"Hello [{get_name_from_peer_id(self.peer_id)}] [{self.peer_id}]!"
        )
        get_logger().info(f"bootnodes: {kwargs.get('bootnodes', [])}")
        get_logger().info(f"Using Model: {self.trainer.model.config.name_or_path}")

        with open(os.path.join(log_dir, f"system_info.txt"), "w") as f:
            f.write(get_system_info())

        # Track accumulated signals for this round
        self.round_signals = 0.0
        self.last_submitted_round = -1  # Track last round we submitted

        # PRG Game
        self.prg_module = PRGModule(log_dir, **kwargs)
        self.prg_game = self.prg_module.prg_game

    def _get_total_rewards_by_agent(self):
        rewards_by_agent = defaultdict(int)
        for stage in range(self.state.stage):
            rewards = self.rewards[stage]
            for agent_id, agent_rewards in rewards.items():
                for batch_id, batch_rewards in agent_rewards.items():
                    tot = 0
                    for generation_rewards in batch_rewards:
                        tot += sum(generation_rewards)
                    rewards_by_agent[agent_id] += tot

        return rewards_by_agent

    def _get_my_rewards(self, signal_by_agent):
        base = 7
        if len(signal_by_agent) == 0:
            return random.randint(base, 14)

        my_signal = signal_by_agent.get(self.peer_id, 0)
        bonus = min(my_signal, 7)
        return random.randint(base + bonus // 2, 14)

    def _submit_to_chain(self, total_signals):
        """Submit accumulated signals to blockchain after round completion"""
        try:
            get_logger().info(f"Submitting round {self.state.round} results to blockchain...")
            get_logger().info(f"Signal by agent: {self._get_total_rewards_by_agent()}")
            get_logger().info(f"Total signals for this round: {total_signals}")
            
            # Submit reward
            self.coordinator.submit_reward(
                self.state.round, 0, int(total_signals), self.peer_id
            )
            get_logger().info(f"Successfully submitted reward to blockchain for round {self.state.round}")

            # Submit winners (using self as max agent for now)
            max_agent = self.peer_id
            self.coordinator.submit_winners(self.state.round, [max_agent], self.peer_id)
            get_logger().info(f"Successfully submitted winners to blockchain for round {self.state.round}")
            
            return True

        except Exception as e:
            get_logger().error(f"Failed to submit round {self.state.round} results to blockchain: {str(e)}")
            get_logger().exception(
                "Failed to submit to chain.\n"
                "This is most likely transient and will recover.\n"
                "There is no need to kill the program.\n"
                "If you encounter this error, please report it to Gensyn by\n"
                "filing a github issue here: https://github.com/gensyn-ai/rl-swarm/issues/ \n"
                "including the full stacktrace."
            )
            return False

    def _hook_after_rewards_updated(self):
        """Accumulate signals during training and submit when round training is done"""
        signal_by_agent = self._get_total_rewards_by_agent()
        current_reward = self._get_my_rewards(signal_by_agent)
        self.round_signals += current_reward
        
        get_logger().debug(f"Accumulated reward: {current_reward}, Total round signals: {self.round_signals}")
        
        # Check if we've completed first stage and haven't submitted yet
        if (self.state.stage >= 1 and 
            self.last_submitted_round < self.state.round):
            
            get_logger().info(f"Round {self.state.round} training completed (stage {self.state.stage})!")
            
            # Submit accumulated signals to blockchain
            submit_success = self._submit_to_chain(self.round_signals)
            
            if submit_success:
                get_logger().info(f"Round {self.state.round} submission completed successfully!")
                self.last_submitted_round = self.state.round
                get_logger().info(f"Skipping remaining training, waiting for next round...")
                # Reset signals after successful submission
                self.round_signals = 0.0
            else:
                get_logger().warning(f"Round {self.state.round} submission failed, but continuing...")

    def _hook_after_round_advanced(self):
        """Called when advancing to next round"""
        get_logger().info(f"Advancing to next round...")
        
        if self.prg_game:
            # TODO: Ideally I think the judge client request question bit should come in the manager and the trainer should be doing only PyTorch-y stuff, 
            # but I have kept it consistent with the evaluate function for now.
            prg_history_dict = self.prg_module.prg_history_dict
            results_dict = self.trainer.play_prg_game_logits(prg_history_dict)
            self.prg_module.play_prg_game(results_dict, self.peer_id)

        # Save to HuggingFace
        self._save_to_hf()
        
        # Reset signals for next round (in case not already reset)
        self.round_signals = 0.0
        get_logger().info(f"Ready for new round training!")

        # Block until swarm round advances
        self.agent_block()

    def _hook_after_game(self):
        """Called after the entire game is completed"""
        get_logger().info("Game completed! Performing final save to HuggingFace...")
        self._save_to_hf()

    def _configure_hf_hub(self, hf_push_frequency):
        username = whoami(token=self.hf_token)["name"]
        model_name = self.trainer.model.config.name_or_path.split("/")[-1]
        model_name += "-Gensyn-Swarm"
        model_name += f"-{self.animal_name}"
        self.trainer.args.hub_model_id = f"{username}/{model_name}"
        self.hf_push_frequency = hf_push_frequency
        get_logger().info("Logging into Hugging Face Hub...")
        login(self.hf_token)

    def _save_to_hf(self):
        if (
            self.hf_token not in [None, "None"]
            and self.state.round % self.hf_push_frequency == 0
        ):
            get_logger().info(f"Pushing model to HuggingFace for round {self.state.round}...")
            try:
                repo_id = self.trainer.args.hub_model_id

                self.trainer.model.push_to_hub(
                    repo_id=repo_id,
                    token=self.hf_token,
                    commit_message=f"rl-swarm: round {self.state.round}, agent {self.animal_name}",
                    tags=[
                        "rl-swarm",
                        "genrl-swarm",
                        "grpo",
                        "gensyn",
                        f"I am {self.animal_name}",
                    ],
                )
                get_logger().info(f"Successfully pushed model to HuggingFace for round {self.state.round}")
            except Exception:
                get_logger().exception(
                    "Failed to push model to the Hugging Face Hub. When you conclude training please try manually pushing it yourself using the instructions here: https://huggingface.co/docs/hub/en/models-uploading",
                    stack_info=True,
                )

    def agent_block(
        self, check_interval=5.0, log_timeout=10.0, max_check_interval=60.0 * 15
    ):
        start_time = time.monotonic()
        fetch_log_time = start_time
        check_backoff = (
            check_interval  # Exponential backoff for already finished rounds.
        )
        while time.monotonic() - start_time < self.train_timeout:
            curr_time = time.monotonic()
            _ = self.communication.dht.get_visible_maddrs(latest=True)

            # Retrieve current round and stage.
            try:
                round_num, stage = self.coordinator.get_round_and_stage()
            except Exception as e:
                if curr_time - fetch_log_time > log_timeout:
                    get_logger().debug(
                        f"Could not fetch round and stage: {e}. Next check in {check_interval}s."
                    )
                    fetch_log_time = curr_time

                time.sleep(check_interval)
                continue

            if round_num >= self.state.round:
                get_logger().info(f"Joining round: {round_num}")
                check_backoff = check_interval  # Reset backoff after successful round
                self.state.round = round_num  # advance to swarm's round.
                return
            else:
                get_logger().info(
                    f"Already finished round: {round_num}. Next check in {check_backoff}s."
                )
                time.sleep(check_backoff)
                check_backoff = min(check_backoff * 2, max_check_interval)

            if round_num == self.max_round - 1:
                return

        get_logger().info("Training timed out!")
