# Mosquitto MQTT Broker Configuration

This directory contains configuration files for the Mosquitto MQTT broker.

## Files

- `mosquitto.conf` - Main Mosquitto configuration file
- `passwd` - Password file for MQTT authentication (create this file)
- `passwd.example` - Example password file template

## Creating Password File

To create a password file for MQTT authentication:

### Option 1: Using Docker (recommended)

```bash
# Create password file with a user
sudo docker exec -it meshtastic-mosquitto mosquitto_passwd -c /mosquitto/config/password.conf username

# Add additional users
sudo docker exec -it meshtastic-mosquitto mosquitto_passwd /mosquitto/config/password.conf another_username
```

### Option 2: Using local mosquitto_passwd

If you have Mosquitto installed locally:

```bash
# Create password file
mosquitto_passwd -c mosquitto/passwd username

# Add additional users
mosquitto_passwd mosquitto/passwd another_username
```

## Directory Structure

```
mosquitto/
├── config/
│   ├── mosquitto.conf    # Main configuration
│   └── passwd            # Password file (create this, not in git)
├── logs/
│   └── mosquitto.log     # Mosquitto logs (auto-created, not in git)
└── data/
    └── (persistence data, auto-created, not in git)
```

## Default Configuration

- **Port**: 1883 (MQTT)
- **Authentication**: Enabled (password file required)
- **Logging**: Enabled to `mosquitto/logs/mosquitto.log`
- **Persistence**: Enabled in `mosquitto/data/`

## Notes

- The password file (`passwd`) is not included in git for security reasons
- Create the password file before starting the containers
- If authentication is not needed, modify `mosquitto.conf` to set `allow_anonymous true`

