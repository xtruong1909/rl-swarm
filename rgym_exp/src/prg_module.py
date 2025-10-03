from enum import Enum
import json
import os
from genrl.logging_utils.global_defs import get_logger
from rgym_exp.src.coordinator import PRGCoordinator


class PRGGameStatus(Enum):
    ERROR = "Error"
    NO_ACTIVE_GAME = "No active game"
    ALREADY_ANSWERED = "Already answered"
    SUCCESS = "Success"


class PRGModule:
    def __init__(self, log_dir, **kwargs):
        prg_game_config = kwargs.get("prg_game_config", None)
        self._prg_game = False
        self.log_dir = log_dir

        # peer_id có thể được truyền vào kwargs, hoặc để None ban đầu
        self.peer_id = kwargs.get("peer_id", None)

        if prg_game_config:
            prg_game = prg_game_config.get("prg_game", False)
            self._prg_game = True if prg_game in [True, "true"] else False
            if self._prg_game:
                modal_proxy_url = prg_game_config.get("modal_proxy_url", None)
                org_id = prg_game_config.get("org_id", None)
                if not modal_proxy_url or not org_id:
                    self._prg_game = False
                    get_logger().debug(
                        "PRG game disabled due to missing configuration."
                    )
                else:
                    self.prg_coordinator = PRGCoordinator(
                        org_id,
                        modal_proxy_url,
                    )
                    self._prg_history_dict = {}
                    self.prg_last_game_claimed = None
                    self.prg_last_game_played = None

    def _set_peer_files(self, peer_id):
        """Thiết lập đường dẫn file cho 1 peer cụ thể"""
        self.peer_id = peer_id
        self.prg_state_file = os.path.join(
            self.log_dir, f"prg_state_{peer_id}.json"
        )
        self.prg_record = os.path.join(
            self.log_dir, f"prg_record_{peer_id}.txt"
        )
        self.load_state()

    def backup_state(self):
        with open(self.prg_state_file, "w") as f:
            json.dump(
                {
                    "prg_history_dict": self._prg_history_dict,
                    "prg_last_game_claimed": self.prg_last_game_claimed,
                    "prg_last_game_played": self.prg_last_game_played,
                },
                f,
            )

    def load_state(self):
        if os.path.exists(self.prg_state_file):
            with open(self.prg_state_file, "r") as f:
                state = json.load(f)
                self._prg_history_dict = {
                    int(k): v for k, v in state["prg_history_dict"].items()
                }
                self.prg_last_game_claimed = state["prg_last_game_claimed"]
                self.prg_last_game_played = state["prg_last_game_played"]
            get_logger().info(
                "Loaded PRG state from file:\n\t"
                f"last game claimed - {self.prg_last_game_claimed},\n\t"
                f"last game played - {self.prg_last_game_played}"
            )

    @property
    def prg_game(self):
        return self._prg_game

    @property
    def prg_history_dict(self):
        return self._prg_history_dict

    def play_prg_game(self, results_dict, peer_id):
        """Chơi 1 ván PRG và lưu log theo peer_id"""
        # thiết lập file cho peer hiện tại
        if self.peer_id != peer_id:
            self._set_peer_files(peer_id)

        status = results_dict.get("status", PRGGameStatus.ERROR)
        if status == PRGGameStatus.SUCCESS:
            if results_dict.get("choice_idx", -1) >= 0:
                current_game = results_dict["game_idx"]
                try:
                    token_balance = self.prg_coordinator.bet_token_balance(peer_id)
                    rounds_remaining = max(1, results_dict["rounds_remaining"])
                    bet_amt = token_balance // rounds_remaining

                    if bet_amt > 0:
                        self.prg_coordinator.guess_answer(
                            current_game,
                            peer_id,
                            results_dict["clue_idx"],
                            results_dict["choice_idx"],
                            bet_amt,
                        )

                    # update lịch sử
                    self._prg_history_dict[current_game] = results_dict["clue_idx"]
                    log_str = (
                        f'Game {current_game} Round {results_dict["clue_idx"]}: '
                        f"Peer {peer_id} placed bet of {bet_amt / 1e18:.2f} tokens "
                        f'on choice - {results_dict["choice"]}\n'
                    )
                    get_logger().info(log_str)
                    with open(self.prg_record, "a") as f:
                        f.write(log_str)
                except Exception as e:
                    get_logger().debug(str(e))

                # nếu sang game mới thì claim game cũ
                if self.prg_last_game_played and current_game != self.prg_last_game_played:
                    try:
                        self.prg_coordinator.claim_reward(
                            self.prg_last_game_played, peer_id
                        )
                        get_logger().info(
                            f"successfully claimed reward for previous game {self.prg_last_game_played}"
                        )
                        with open(self.prg_record, "a") as f:
                            f.write(
                                f"successfully claimed reward for previous game {self.prg_last_game_played}\n"
                            )
                        self.prg_last_game_claimed = self.prg_last_game_played
                    except Exception as e:
                        get_logger().debug(str(e))

                self.prg_last_game_played = current_game
                self.backup_state()

        elif status == PRGGameStatus.NO_ACTIVE_GAME:
            # game kết thúc, thử claim nếu chưa
            if (
                self.prg_last_game_played
                and self.prg_last_game_played != self.prg_last_game_claimed
            ):
                try:
                    self.prg_coordinator.claim_reward(
                        self.prg_last_game_played, peer_id
                    )
                    get_logger().info(
                        f"successfully claimed reward for previous game {self.prg_last_game_played}"
                    )
                    with open(self.prg_record, "a") as f:
                        f.write(
                            f"successfully claimed reward for previous game {self.prg_last_game_played}\n"
                        )
                    self.prg_last_game_claimed = self.prg_last_game_played
                    self.prg_last_game_played = None
                    self.backup_state()
                except Exception as e:
                    get_logger().debug(str(e))
