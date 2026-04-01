#!/usr/bin/env bash
# 오류 발생 시 즉시 중단
set -o errexit

# 1. 파이썬 라이브러리 설치 (requirements.txt)
pip install -r requirements.txt

# 2. 시스템 패키지 업데이트 및 wkhtmltopdf 설치
# Render의 우분투 환경에서 PDF 엔진을 설치하는 핵심 명령어입니다.
apt-get update && apt-get install -y wkhtmltopdf