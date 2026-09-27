#!/usr/bin/env bash
set -o errexit

PROJECT_DIR="/var/www/sst"
VENV_BIN="$PROJECT_DIR/review/bin"

sudo cp "$PROJECT_DIR/deploy/ai-code-reviewer-gunicorn.service" /etc/systemd/system/
sudo cp "$PROJECT_DIR/deploy/ai-code-reviewer-celery.service" /etc/systemd/system/

sudo systemctl daemon-reload

sudo -u www-data HOME="$PROJECT_DIR" "$VENV_BIN/python" manage.py collectstatic --no-input
sudo -u www-data HOME="$PROJECT_DIR" "$VENV_BIN/python" manage.py migrate

sudo systemctl enable --now redis-server
sudo systemctl enable --now ai-code-reviewer-gunicorn
sudo systemctl enable --now ai-code-reviewer-celery

sudo systemctl status ai-code-reviewer-gunicorn --no-pager
sudo journalctl -u ai-code-reviewer-celery -n 20 --no-pager