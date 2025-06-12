import logging
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from pythonjsonlogger import jsonlogger

# Add the parent directory to the Python path
parent_dir = Path(__file__).parent.parent
sys.path.append(str(parent_dir))

# Note: We're patching hivemind_exp functions (rewards_key, get_dht_value, get_name_from_peer_id)
# because these functions are only copied over at build time in Docker and aren't available during local testing.
# This allows us to test the DHTPublisher class without needing the actual hivemind_exp module.

from api.dht_pub import GossipDHTPublisher
from api.game_tree import Payload, WorldState, to_bytes, from_bytes
from api.kinesis import GossipMessage, GossipMessageData

# Wraps data put into the DHT so we can mock the DHT.get method
class DummyValue:
    def __init__(self, value):
        self.value = value

class TestGossipDHTPublisher:
    """Tests for the GossipDHTPublisher class."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create mock objects
        self.mock_dht = MagicMock()
        self.mock_kinesis = MagicMock()

        # Create a real logger for testing
        self.mock_logger = logging.getLogger("test_logger")
        self.mock_logger.setLevel(logging.INFO)

        # Add a handler to the logger so caplog can capture the logs
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)

        # Use the JSON formatter from python-json-logger
        json_formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(message)s %(extra)s"
        )
        handler.setFormatter(json_formatter)
        self.mock_logger.addHandler(handler)

        self.coordinator = MagicMock()

        # Create the publisher with a short poll interval for testing
        self.publisher = GossipDHTPublisher(
            dht=self.mock_dht,
            kinesis_client=self.mock_kinesis,
            logger=self.mock_logger,
            poll_interval_seconds=0.1,
            coordinator=self.coordinator,
        )

    def teardown_method(self):
        """Clean up after tests."""
        # Stop the publisher if it's running
        if self.publisher.running:
            self.publisher.stop()
            # Give it a moment to stop
            time.sleep(0.2)

    def test_initialization(self, caplog):
        """Test that the publisher initializes correctly."""
        # Set the caplog level to capture INFO messages
        caplog.set_level(logging.INFO)

        # Re-initialize the publisher to capture the logs
        self.publisher = GossipDHTPublisher(
            dht=self.mock_dht,
            kinesis_client=self.mock_kinesis,
            logger=self.mock_logger,
            poll_interval_seconds=0.1,
            coordinator=self.coordinator,
        )

        # Verify the log message was captured
        assert len(caplog.records) > 0
        assert caplog.records[0].message == "GossipDHTPublisher initialized"

        # Verify other properties
        assert self.publisher.dht == self.mock_dht
        assert self.publisher.kinesis_client == self.mock_kinesis
        assert self.publisher.coordinator == self.coordinator
        assert self.publisher.poll_interval_seconds == 0.1
        assert self.publisher.current_round == -1
        assert self.publisher.current_stage == -1
        assert self.publisher.last_polled is None
        assert self.publisher.running is False
        assert self.publisher._poll_thread is None

    def test_poll_once_no_change(self, caplog):
       """Test polling when there's no round/stage change."""
       # Set up the coordinator mock to return the same round/stage
       self.coordinator.get_round_and_stage.return_value = (1, 1)
       self.publisher.current_round = 1
       self.publisher.current_stage = 1

       # Poll once
       self.publisher._poll_once()

       # Check that get_round_and_stage was called on the coordinator
       self.coordinator.get_round_and_stage.assert_called_once()

       # Check that the round/stage didn't change
       assert self.publisher.current_round == 1
       assert self.publisher.current_stage == 1

       # Check that the logger was called
       assert len(caplog.records) > 0
       assert caplog.records[0].message == "Polled for round/stage"
       assert caplog.records[0].round == 1
       assert caplog.records[0].stage == 1

       # Check that last_polled was updated
       assert self.publisher.last_polled is not None

    def test_poll_once_with_change(self, caplog):
       """Test polling when there's a round/stage change."""
       # Set up the coordinator mock to return a different round/stage
       self.coordinator.get_round_and_stage.return_value = (2, 1)
       self.publisher.current_round = 1
       self.publisher.current_stage = 1

       # Mock the _publish_gossip method
       self.publisher._publish_gossip = MagicMock()

       # Mock the dht.get method to return a tuple of two empty dictionaries
       self.publisher.dht.get = MagicMock(return_value=({}, {}))

       # Poll once
       self.publisher._poll_once()

       # Check that get_round_and_stage was called on the coordinator
       self.coordinator.get_round_and_stage.assert_called_once()

       # Check that the round/stage changed
       assert self.publisher.current_round == 2
       assert self.publisher.current_stage == 1

       # Check that _publish_gossip was called
       self.publisher._publish_gossip.assert_called_once()

       # Check that the logger was called
       assert len(caplog.records) > 0
       assert caplog.records[0].message == "Polled for round/stage"
       assert caplog.records[0].round == 2
       assert caplog.records[0].stage == 1

       # Check that last_polled was updated
       assert self.publisher.last_polled is not None

    def test_poll_once_error(self, caplog):
       """Test polling when there's an error."""
       # Set up the coordinator mock to raise an exception
       self.coordinator.get_round_and_stage.side_effect = Exception("Test error")

       # Poll once
       self.publisher._poll_once()

       # Check that get_round_and_stage was called on the coordinator
       self.coordinator.get_round_and_stage.assert_called_once()

       # Check that the round/stage didn't change
       assert self.publisher.current_round == -1
       assert self.publisher.current_stage == -1

       # Check that the logger was called with the error
       assert len(caplog.records) > 0
       assert caplog.records[0].message == "Error polling for round/stage in gossip"
       assert caplog.records[0].error == "Test error"

       # Check that last_polled was not updated
       assert self.publisher.last_polled is None

    def test_publish_gossip(self, caplog):
       """Test publishing gossip data."""
       # Set up test data
       gossip_data = [
           (
               1000.0,
               {
                   "id": "id1",
                   "message": "message1",
                   "node": "node1",
                   "nodeId": "peer_id_1",
                   "dataset": "math",
               },
           ),
           (
               1001.0,
               {
                   "id": "id2",
                   "message": "message2",
                   "node": "node2",
                   "nodeId": "peer_id_2",
               },
           ),
       ]

       # Mock the Kinesis client's put_gossip method
       self.publisher.kinesis_client.put_gossip = MagicMock()

       # Publish gossip
       self.publisher._publish_gossip(gossip_data)

       # Check that put_gossip was called
       self.publisher.kinesis_client.put_gossip.assert_called_once()

       # Get the actual message that was passed to put_gossip
       actual_message = self.publisher.kinesis_client.put_gossip.call_args[0][0]

       # Check that the message has the correct type
       assert actual_message.type == "gossip"

       # Check that the message has the correct data
       assert len(actual_message.data) == 2

       # Check the first data item
       assert actual_message.data[0].id == "id1"
       assert actual_message.data[0].message == "message1"
       assert actual_message.data[0].peer_name == "node1"
       assert actual_message.data[0].peer_id == "peer_id_1"
       assert actual_message.data[0].dataset == "math"

       # Check the second data item
       assert actual_message.data[1].id == "id2"
       assert actual_message.data[1].message == "message2"
       assert actual_message.data[1].peer_name == "node2"
       assert actual_message.data[1].peer_id == "peer_id_2"
       assert actual_message.data[1].dataset is None

       # Check that the logger was called
       assert len(caplog.records) > 0
       assert caplog.records[0].message == "Publishing gossip messages"
       assert caplog.records[0].num_messages == 2

    def test_publish_gossip_no_data(self, caplog):
       """Test publishing gossip when there's no data."""
       # Mock the Kinesis client's put_gossip method
       self.publisher.kinesis_client.put_gossip = MagicMock()

       # Publish gossip with empty list
       self.publisher._publish_gossip([])

       # Check that put_gossip was not called
       assert self.publisher.kinesis_client.put_gossip.call_count == 0

       # Check that the logger was called
       assert len(caplog.records) > 0
       assert caplog.records[0].message == "No gossip data to publish"

    def test_publish_gossip_with_real_payload(self, caplog):
        """Test publishing gossip with a real payload from the DHT."""
        # Set up test data
        payload = Payload(
            world_state=WorldState(
                environment_states = {
                    'question': 'What day of the week was July 10, 2023? Give the weekday name in full.',
                    'answer': 'Monday',
                    'metadata': {
                        'dataset_index': 0,
                        'difficulty': {
                            'num_digits': None,
                            'num_terms': None,
                            'offset_upper_bound': 100,
                            'tasks': ['count_days', 'weekday_of_date', 'is_leap_year', 'recurring_event_day']
                        },
                        'expression': None,
                        'num_digits': None,
                        'num_terms': None,
                        'source_dataset': 'calendar_arithmetic',
                        'source_index': 0,
                        'split': 'train',
                        'target_date': '2023-07-10',
                        'task': 'weekday_of_date'
                    }
                },
                opponent_states = None,
                personal_states = None
            ),
            actions=[
                'To determine the day of the week for July 10, 2023:\n\n1. **Understand the Problem**: We need to calculate the date and determine its corresponding weekday in the given calendar year.\n   \n2. **Calculate the Day**: Start from the given date and observe the sequential days of counting in January, February, March, and so on until we reach July 10.\n\n3. **Check for Leap Year**: Determine if the year follows a leap year (divisible by 4, but not by 100, but on every 400s, the year is also divisible by 4) or not to account for the 2024 Bianchi number problem.\n\n4. **Convert Month to Days**: October has 31 days, November has 30, December has 31, and April and May have 31 days. July has 31.\n\n5. **Determine the Day**:\n   - June 30, 2023 (6th day of June, not the preceding day)\n   - The 10th day is the 11th (6) of July, making it the 11th of July.\n\n6. **',
                'To determine the weekday for July 10, 2023, I will perform the following steps:\n\n1. Identify the day of the year (i.e., the number of days from June 30 to July 10).\n2. What is the day of the week 365 days after the start of summer (June 30)?\n3. What is the day of the week 364 days after the start of summer?\n4. What is the day of the week 363 days after the start of summer?\n5. Continue this process until you reach July 10.\n\nThus, the beginning of summer in 2023 is June 30. Since there are 365 days from June 30 to July 10:\n365 days is 21 weeks and 7 out of the 30 days. Therefore, 21 weeks from June 30 is:\n\n- 21 weeks ends in z.\n- The first day after January 1 is:\n  - 2006/12/21 (Note: January 1 was 6/1 which is not a full month, it\'s a day of the week reference'
            ],
            metadata=None
        )

        # Mock the coordinator to return a new round
        self.coordinator.get_round_and_stage.return_value = (2, 1)
        self.publisher.current_round = 1
        self.publisher.current_stage = 1

        # Mock the DHT to return our test payload
        self.publisher.dht.get = MagicMock(return_value=(
            {'test_peer_id': DummyValue(to_bytes(payload))},
            {}
        ))

        # Mock the Kinesis client's put_gossip method
        self.publisher.kinesis_client.put_gossip = MagicMock()

        # Poll once
        self.publisher._poll_once()

        # Check that put_gossip was called
        self.publisher.kinesis_client.put_gossip.assert_called_once()

        # Get the actual message that was passed to put_gossip
        actual_message = self.publisher.kinesis_client.put_gossip.call_args[0][0]

        # Check that the message has the correct type
        assert actual_message.type == "gossip"

        # Check that the message has the correct data
        assert len(actual_message.data) == 1

        # Check the data item
        data_item = actual_message.data[0]
        assert data_item.id is not None  # Should be a hash
        assert data_item.message.startswith("What day of the week was July 10, 2023?")  # Should start with the question
        assert "To determine the" in data_item.message  # Should contain the answer
        assert data_item.peer_name is not None  # Should be derived from peer_id
        assert data_item.peer_id == "test_peer_id"
        assert data_item.dataset == "calendar_arithmetic"  # Should be from metadata