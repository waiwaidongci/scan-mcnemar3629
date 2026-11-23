import logging
import time
from pathlib import Path
from src.services.execution import claim, execute, materialize_due
from src.services.replay_execution import claim_replay, execute_replay
from src.utils.wakeup import wait

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
heartbeat = Path('/tmp/analysis-heartbeat')


def main():
    while True:
        try:
            materialize_due()
            lease = claim()
            heartbeat.touch()
            if lease:
                execute(lease)
                heartbeat.touch()
                continue
            replay_lease = claim_replay()
            if replay_lease:
                execute_replay(replay_lease)
                heartbeat.touch()
            else:
                wait()
        except Exception:
            log.exception('Worker cycle failed')
            time.sleep(2)


if __name__ == '__main__':
    main()
