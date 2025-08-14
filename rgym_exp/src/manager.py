import logging
import os
import sys
import time
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

from rgym_exp.src.coordinator import PRGCoordinator
from rgym_exp.src.utils.name_utils import get_name_from_peer_id
from rgym_exp.src.trainer import PRGGameStatus

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
        format_msg = f"[{self.animal_name}] %(asctime)s %(levelname)s: %(message)s"
        logging.basicConfig(level=logging.INFO, format=format_msg)
        formatter = logging.Formatter(format_msg)
        file_handler = logging.FileHandler(
            os.path.join(log_dir, f"training_{self.animal_name}.log")
        )
        file_handler.setFormatter(formatter)
        _LOG = get_logger()
        _LOG.addHandler(file_handler)

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
            f"🐱 Hello 🐈 [{get_name_from_peer_id(self.peer_id)}] 🦮 [{self.peer_id}]!"
        )
        get_logger().info(f"bootnodes: {kwargs.get('bootnodes', [])}")
        get_logger().info(f"Using Model: {self.trainer.model.config.name_or_path}")

        with open(os.path.join(log_dir, f"system_info.txt"), "w") as f:
            f.write(get_system_info())

        self.batched_signals = 0.0
        self.time_since_submit = time.time()  # seconds
        self.submit_period = 3.0  # hours
        self.submitted_this_round = False

        # PRG Game
        self.prg_game = False
        prg_game_config = kwargs.get("prg_game_config", None)
        if prg_game_config:
            self.prg_game = prg_game_config.get("prg_game", False)
            if self.prg_game:
                modal_proxy_url = prg_game_config.get("modal_proxy_url", None)
                org_id = prg_game_config.get("org_id", None)
                if (
                    not modal_proxy_url
                    or not org_id
                ):
                    self.prg_game = False
                    get_logger().debug(
                        "PRG game disabled due to missing configuration."
                    )
                else:
                    self.prg_coordinator = PRGCoordinator(
                        org_id,
                        modal_proxy_url,
                    )
                    self.prg_history_dict = {}
                    self.prg_last_game_claimed = None
                    self.prg_last_game_played = None
                    self.prg_record = log_dir + '/prg_record.txt'

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
        if len(signal_by_agent) == 0:
            return 0
        if self.peer_id in signal_by_agent:
            my_signal = signal_by_agent[self.peer_id]
        else:
            my_signal = 0
        my_signal = (my_signal + 1) * (my_signal > 0) + my_signal * (my_signal <= 0)
        return my_signal

    def _try_submit_to_chain(self, signal_by_agent):
        elapsed_time_hours = (time.time() - self.time_since_submit) / 3600
        if elapsed_time_hours > self.submit_period:
            try:
                self.coordinator.submit_reward(
                    self.state.round, 0, int(self.batched_signals), self.peer_id
                )
                self.batched_signals = 0.0
                if len(signal_by_agent) > 0:
                    max_agent, max_signal = max(
                        signal_by_agent.items(), key=lambda x: x[1]
                    )
                else:  # if we have no signal_by_agents, just submit ourselves.
                    max_agent = self.peer_id

                self.coordinator.submit_winners(
                    self.state.round, [max_agent], self.peer_id
                )
                self.time_since_submit = time.time()
                self.submitted_this_round = True
            except Exception as e:
                get_logger().debug(str(e))

    def _hook_after_rewards_updated(self):
        signal_by_agent = self._get_total_rewards_by_agent()
        self.batched_signals += self._get_my_rewards(signal_by_agent)
        self._try_submit_to_chain(signal_by_agent)

    def _hook_after_round_advanced(self):
        if self.prg_game:
            # TODO: Ideally I think the judge client request question bit should come in the manager and the trainer should be doing only PyTorch-y stuff, 
            # but I have kept it consistent with the evaluate function for now.
            results_dict = self.trainer.play_prg_game_logits(self.prg_history_dict)
            status = results_dict.get('status', PRGGameStatus.ERROR)
            if status == PRGGameStatus.SUCCESS:
                if results_dict.get('choice_idx', -1) >= 0:
                    current_game = results_dict['game_idx']
                    try:
                        token_balance = self.prg_coordinator.bet_token_balance(self.peer_id)
                        rounds_remaining = max(1, results_dict['rounds_remaining'])
                        bet_amt = token_balance // rounds_remaining

                        if bet_amt > 0:
                            self.prg_coordinator.guess_answer(current_game, self.peer_id, results_dict['clue_idx'], results_dict['choice_idx'], bet_amt)
                        # only update if we successfully played this round
                        self.prg_history_dict[current_game] = results_dict["clue_idx"]
                        log_str = f'Game {current_game} Round {results_dict["clue_idx"]}: Agent {self.peer_id} placed bet of {bet_amt / 1e18:.2f} tokens on choice - {results_dict["choice"]}\n'
                        get_logger().info(log_str)
                        with open(self.prg_record, 'a') as f:
                            f.write(log_str)
                    except Exception as e:
                        get_logger().debug(str(e))

                    # new game has started, claim rewards for previous game.
                    if self.prg_last_game_played and current_game != self.prg_last_game_played:
                        try:
                            self.prg_coordinator.claim_reward(self.prg_last_game_played, self.peer_id)
                            get_logger().info(f'successfully claimed reward for previous game {self.prg_last_game_played}')
                            with open(self.prg_record, 'a') as f:
                                f.write(f'successfully claimed reward for previous game {self.prg_last_game_played}\n')
                            # only update if we successfully claimed the reward
                            self.prg_last_game_claimed = self.prg_last_game_played
                        except Exception as e:
                            get_logger().debug(str(e))
                    
                    self.prg_last_game_played = current_game


            # Game Finished, claim rewards for previous game
            elif status == PRGGameStatus.NO_ACTIVE_GAME:
                # at somepoint we have made a bet but we never claimed the reward
                if self.prg_last_game_played and self.prg_last_game_played != self.prg_last_game_claimed:
                    try:
                        self.prg_coordinator.claim_reward(self.prg_last_game_played, self.peer_id)
                        get_logger().info(f'successfully claimed reward for previous game {self.prg_last_game_played}')
                        with open(self.prg_record, 'a') as f:
                            f.write(f'successfully claimed reward for previous game {self.prg_last_game_played}\n')
                        # only update if we successfully claimed the reward
                        self.prg_last_game_claimed = self.prg_last_game_played
                        self.prg_last_game_played = None
                    except Exception as e:
                        get_logger().debug(str(e))

        self._save_to_hf()

        # Try to submit to chain again if necessary, but don't update our signal twice
        if not self.submitted_this_round:
            signal_by_agent = self._get_total_rewards_by_agent()
            self._try_submit_to_chain(signal_by_agent)

        # Reset flag for next round
        self.submitted_this_round = False

        # Block until swarm round advances
        self.agent_block()

    def _hook_after_game(self):
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
            get_logger().info(f"pushing model to huggingface")
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
                get_logger().info(f"🐝 Joining round: {round_num}")
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
