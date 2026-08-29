import pyautogui
import time
import pyperclip

ref = open("ref.txt", "r", encoding="utf-8")

def run():
    time.sleep(6)
    position = [1600, 590]
    pyautogui.moveTo(position[0], position[1], duration=0)
    time.sleep(0.1)
    pyautogui.click()
    time.sleep(0.5)

    # paste in the text in ref
    pyperclip.copy(ref.read())
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.5)
    pyautogui.click()
    time.sleep(2)