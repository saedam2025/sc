#!/usr/bin/env bash
# 오류 발생 시 즉시 중단
set -o errexit

# 1. 라이브러리 설치
pip install -r requirements.txt

# 2. PDF 엔진 wkhtmltopdf 설치
apt-get update && apt-get install -y wkhtmltopdf