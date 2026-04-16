import requests

url = \"https://api.fintel.io/web/v/0.0/ss/us/tsla\"

headers = {\"accept\": \"application/json\"}

response = requests.get(url, headers=headers)

print(response.status_code)
print(response.text[:500])