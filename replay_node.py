"""Single-board second-node simulator.

Run on the Pi for a demo:
    python3 replay_node.py --scenario fall

It publishes the same MQTT contract as a second Raspberry Pi without
touching GPIO or the real sensor process.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

from edge_bus import EdgeBus


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", default="pi-02")
    parser.add_argument("--room-id", default="living-room")
    parser.add_argument("--scenario", choices=("heartbeat", "fall"), default="fall")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    outbox = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{args.device_id}_outbox.jsonl")
    bus = EdgeBus(room_id=args.room_id, device_id=args.device_id, outbox_path=outbox)
    bus.start()
    # A one-shot demo should visibly reach the local broker when available.
    # Keep the offline outbox fallback, but allow the async MQTT handshake a
    # short window before publishing and exiting.
    if args.once:
        deadline = time.time() + 3.0
        while not bus.connected and time.time() < deadline:
            time.sleep(0.05)
    seq = 0
    try:
        while True:
            seq += 1
            now = datetime.now(timezone.utc).isoformat()
            bus.publish_json("heartbeat", {
                "schema_version": 1,
                "device_id": args.device_id,
                "status": "online",
                "seq": seq,
                "updated_at": now,
            }, retain=True)
            bus.publish_json("state", {
                "schema_version": 1,
                "device_id": args.device_id,
                "room_id": args.room_id,
                "seq": seq,
                "presence_state": args.scenario == "fall",
                "radar_targets": [{"dist": 1.2, "x": 120, "y": 1350, "speed": 0}],
                "detector": {"state": "STILL", "confidence": 0.86, "model_version": "replay-v1"},
                "updated_at": now,
            })
            if args.scenario == "fall":
                event = {
                    "schema_version": 1,
                    "event_id": f"{args.device_id}-{int(time.time() * 1000)}",
                    "correlation_key": f"{args.room_id}:FALL:{int(time.time() // 10)}",
                    "device_id": args.device_id,
                    "event_type": "FALL",
                    "type": "FALL",
                    "time": now,
                    "occurred_at": now,
                    "confidence": 0.86,
                    "model_version": "replay-v1",
                    "evidence": {"source": "replay", "scenario": "fall"},
                    "detail": "replay node simulated fall",
                }
                bus.publish_json("event", event)
            if args.once:
                deadline = time.time() + 2.0
                while bus.connected and len(bus.outbox) and time.time() < deadline:
                    time.sleep(0.05)
                break
            time.sleep(10)
    finally:
        bus.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
