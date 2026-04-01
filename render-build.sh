#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt

# wkhtmltopdf 설치 (Render의 우분투 환경용)
apt-get update && apt-get install -y wkhtmltopdf