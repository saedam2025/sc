#!/usr/bin/env bash
# 오류 발생 시 즉시 중단
set -o errexit

# 1. 라이브러리 설치
pip install -r requirements.txt

# 2. PDF 엔진 wkhtmltopdf 설치
apt-get update && apt-get install -y wkhtmltopdf

# 3. OpenCV(headless)가 쓰는 공유 라이브러리.
#    이 라이브러리가 없으면 import cv2가 실패해 이력서 얼굴 인식만 조용히 꺼진다.
#    배포판마다 패키지 이름이 달라 설치에 실패해도 배포는 계속한다.
apt-get install -y libglib2.0-0t64 || apt-get install -y libglib2.0-0 || true
apt-get install -y libgomp1 || true
