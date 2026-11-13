# house-beat

Watches a heartbeat sent by a private home system and sends one alert if it stops (and one line when it is back).

- `outside_beat.py`: the checker (Python standard library only).
- `.github/workflows/house-beat.yml`: runs the checker once an hour. It does nothing unless the repository variable `HOUSE_ALARM_ON` is `true`.

This repo is public only so that scheduled runs are free. It holds no data and no secrets. The heartbeat itself is a private repository
variable and the alert address is a private repository secret: neither can be read from here.

The one thing visible is the branch `house-beat-state`: a small file with an "alert open" marker and the times of recent alerts.
That is accepted.
