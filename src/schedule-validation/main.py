# SPDX-FileCopyrightText: MAS4TE Developers
#
# SPDX-License-Identifier: MIT

"""MQTT listener that validates fulfillment schedules against the database."""

from __future__ import annotations

import json
import logging
import os
import sqlite3

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

import util

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = os.getenv("MQTT_PORT")
MQTT_USER = os.getenv("MQTT_USER")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")
ACTUAL_SCHEDULES_TOPIC = os.getenv("ACTUAL_SCHEDULES_TOPIC")

DB_PATH = os.getenv("DB_PATH")


def _on_connect(
    client: mqtt.Client,
    userdata: dict,
    reason_code,
) -> None:
    if reason_code != 0:
        logger.error("MQTT connect failed with reason code %s", reason_code)
        return
    topic = userdata["actual_schedules_topic"]
    client.subscribe(topic, qos=1)
    logger.info("Subscribed to actual schedules topic: %s", topic)


def _on_message(client: mqtt.Client, userdata: dict, msg: mqtt.MQTTMessage) -> None:
    conn = userdata["db_conn"]
    result = util.process_actual_schedule(conn, msg.payload)

    client.publish(
        topic=msg.topic,
        payload=json.dumps(result),
        qos=1,
    )


def main() -> None:
    util.require_env_vars(
        [
            ("MQTT_HOST", MQTT_HOST),
            ("MQTT_PORT", MQTT_PORT),
            ("MQTT_USER", MQTT_USER),
            ("MQTT_PASSWORD", MQTT_PASSWORD),
            ("ACTUAL_SCHEDULES_TOPIC", ACTUAL_SCHEDULES_TOPIC),
            ("DB_PATH", DB_PATH),
        ]
    )
    mqtt_port = int(MQTT_PORT)

    conn = sqlite3.connect(DB_PATH)
    util.create_actual_schedules_table(conn)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.tls_set()

    client.user_data_set(
        {
            "db_conn": conn,
            "actual_schedules_topic": ACTUAL_SCHEDULES_TOPIC,
        }
    )
    client.on_connect = _on_connect
    client.on_message = _on_message

    logger.info(
        "Connecting to MQTT broker %s:%s",
        MQTT_HOST,
        mqtt_port,
    )
    client.connect(MQTT_HOST, mqtt_port)
    client.loop_forever()


if __name__ == "__main__":
    main()
