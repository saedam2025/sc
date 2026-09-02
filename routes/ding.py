import wave
import math
import struct

# 설정
SAMPLE_RATE = 44100
FILE_NAME = "dingdong.wav"

def generate_tone(frequency, duration, volume=0.5):
    num_samples = int(SAMPLE_RATE * duration)
    audio_data = []
    
    for i in range(num_samples):
        # 사인파 생성
        t = float(i) / SAMPLE_RATE
        sample = volume * math.sin(2.0 * math.pi * frequency * t)
        
        # 소리가 자연스럽게 줄어들도록 여운(Fade out) 주기
        fade = 1.0 - (i / num_samples)
        sample *= (fade ** 2) # 곡선 형태로 부드럽게 감소
        
        # 16비트 오디오 데이터로 변환
        audio_data.append(int(sample * 32767.0))
        
    return audio_data

# '띵' (높은 음: 솔), 0.5초
ding = generate_tone(783.99, 0.5)
# '동' (낮은 음: 미), 0.8초
dong = generate_tone(659.25, 0.8)

# 띵동 합치기
dingdong = ding + dong

# WAV 파일로 저장
print("알람음 생성 중...")
with wave.open(FILE_NAME, 'w') as wav_file:
    wav_file.setnchannels(1)      # 모노
    wav_file.setsampwidth(2)      # 16비트
    wav_file.setframerate(SAMPLE_RATE)
    
    for sample in dingdong:
        wav_file.writeframes(struct.pack('<h', sample))

print(f"완료! '{FILE_NAME}' 파일이 만들어졌습니다. 재생해 보세요!")