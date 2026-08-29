import requests
from bs4 import BeautifulSoup

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
    prod_id = link.split('/')[-1]
    return prod_id

def get_datasheet(prod_id):
    datasheet_url = f"{API_URL}/datasheet/{prod_id}.pdf"
    response = requests.get(datasheet_url, headers=HEADERS)
    with open(f"{prod_id}.pdf", "wb") as f:
        f.write(response.content)

id = lcsc_search("STM32F103C8T6")