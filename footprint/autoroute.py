import pyautogui
import time

# while True:
#     x, y = pyautogui.position()
#     print(f"Mouse position: ({x}, {y})")
#     time.sleep(0.2)

positions = [
    [1969, 73],
    [2144, 912],
    [2600, 1044]
]

time.sleep(3)
for pos in positions:
    pyautogui.moveTo(pos[0], pos[1], duration=0)
    time.sleep(0.1)
    pyautogui.click()
time.sleep(0.2)