import requests
from bs4 import BeautifulSoup
import ast

API_URL = "https://lcsc.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3"
}

def lcsc_search(item):
    search_url = f"{API_URL}/search?q={item}"
    response = requests.get(search_url, headers=HEADERS)
    if response.status_code != 200:
        return None

    soup = BeautifulSoup(response.text, 'html.parser')

    # get the first result:
    # 1) find the table with className "tableContentTable"
    # 2) find the first row
    # 3) find the second column
    table = soup.find('table', class_='tableContentTable')
    if not table:
        raise ValueError("Table not found!")
    first_row = table.find('tbody').find('tr')
    if not first_row:
        raise ValueError("No results found")
    second_column = first_row.find_all('td')[1]
    # get the link inside the second column
    link = second_column.find('a')['href']
    print(link)
    product_ID = link[len("https://www.lcsc.com/product-detail/"):len(link)-5]
    return product_ID

# print(lcsc_search("40uf"))

def download_datasheet(product_ID):
    url = f"https://wmsc.lcsc.com/wmsc/upload/file/pdf/v2/{product_ID}.pdf"
    print(url)

    response = requests.get(url,headers=HEADERS)

    with open(f'{product_ID}_datasheet.pdf', 'wb') as f:
        f.write(response.content)
    print("Datasheet",product_ID,"downloaded!")

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
    with open(f'{product_ID}_image.jpg', 'wb') as f:
        f.write(image_response.content)
    print("Image",product_ID,"downloaded!")


# download_datasheet(lcsc_search("22uf"))
# download_image(lcsc_search("STM32"))