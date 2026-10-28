import time
import json
import threading
import paho.mqtt.client as mqtt

MQTT_BROKER = "127.0.0.1"
MQTT_PORT = 1883
POLL_INTERVAL = 10 # Segundos

device_id = None
poller_thread = None

def poll_inverter(client):
    global device_id
    while True:
        if device_id:
            topic = f"dtu/{device_id}/sub/service/dev_rpc"
            payload = {
                "c": 5,
                "t": str(int(time.time())),
                "s": "active_poller",
                "i": 502,
                "b": {}
            }
            client.publish(topic, json.dumps(payload))
            print(f"[ACTIVE POLLER] Sent Batch Read to {topic}")
        time.sleep(POLL_INTERVAL)

def on_connect(client, userdata, flags, rc):
    print(f"Connected to Mosquitto with result code {rc}")
    client.subscribe("dtu/+/pub/event/dev_event_post")

def on_message(client, userdata, msg):
    global device_id, poller_thread
    if not device_id:
        try:
            parts = msg.topic.split('/')
            if len(parts) >= 2 and parts[0] == 'dtu':
                device_id = parts[1]
                print(f"[ACTIVE POLLER] Discovered Device ID: {device_id}")
                poller_thread = threading.Thread(target=poll_inverter, args=(client,), daemon=True)
                poller_thread.start()
        except Exception as e:
            print(f"Error parsing topic: {e}")

if __name__ == "__main__":
    import os
    MQTT_BROKER = os.environ.get("MQTT_HOST", "127.0.0.1")
    MQTT_USER = os.environ.get("MQTT_USER", "")
    MQTT_PASS = os.environ.get("MQTT_PASSWORD", "")

    client = mqtt.Client()
    if MQTT_USER and MQTT_PASS:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
        
    client.on_connect = on_connect
    client.on_message = on_message
    
    while True:
        try:
            print(f"Connecting to {MQTT_BROKER}:{MQTT_PORT}...")
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            break
        except Exception as e:
            print(f"Failed to connect to {MQTT_BROKER}: {e}. Retrying in 5s...")
            time.sleep(5)
            
    client.loop_forever()
