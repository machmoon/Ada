import requests
from bs4 import BeautifulSoup
import ast
import os
import massive

API_URL = "https://lcsc.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
}

URL = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/parse")
# PDF_PATH = os.getenv("TEST_PDF", "datasheet.pdf")

names = ["C23922", "C26350", "C2765186"]

def download_image(product_ID):
    url = f"https://www.lcsc.com/product-image/{product_ID}.html"

    response = requests.get(url, headers=HEADERS)
    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, 'html.parser')

    scripts = soup.find_all("script")

    # find image link
    # find 4th instance of <script>, convert contents into a dict
    contents_dict = ast.literal_eval(scripts[3].string)
    image_link = contents_dict["contentUrl"]
    print(image_link)

    # download the image from the image link
    image_response = requests.get(image_link, headers=HEADERS)
    with open(f'{product_ID}.jpg', 'wb') as f:
        f.write(image_response.content)
    print("Image",product_ID,"downloaded!")

for name in names:
    download_image(name)

# check if there is already file:
done = False
for i in range(len(names)):
    if os.path.exists(names[i] + ".json"):
        with open(names[i] + ".json", "r") as f:
            with open("adjacency_" + str(i) + ".json", "w") as f2:
                f2.write(f.read())
        continue
    else:
        r = requests.post(URL + "?pdfUrl=https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/" + names[i] + ".pdf&part_name=" + names[i], timeout=300)
        print("Status:", r.status_code)
        body = r.json()
        structured = body.get("structured")
        with open("adjacency_" + str(i) + ".json", "w") as f:
            f.write(structured)
        with open(names[i] + ".json", "w") as f2:
            f2.write(structured)
        f.close()
        f2.close()
        break
    if i == len(names) - 1:
        done = True

if (done):
    massive.main()

