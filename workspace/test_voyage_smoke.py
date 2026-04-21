import os
from dotenv import load_dotenv
load_dotenv()

import voyageai
client = voyageai.Client(api_key=os.getenv('VOYAGE_API_KEY'))
res = client.embed(model='voyage-4-large', input=['smoke test 2026-04-21'])
print('✅ Voyage-4-large SMOKE | Dim:', len(res[0]['embedding']))
print('Model:', res[0].get('model', 'voyage-4-large'))
