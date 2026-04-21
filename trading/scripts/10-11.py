import sys
import json
import os
import requests
from ddgs import DDGS

def main(topic):
    # Step 1: Gather
    web_results = gather_web(topic)
    twitter_results = gather_twitter(topic)
    news_results = gather_news(topic)
    if not isinstance(twitter_results, list):
        twitter_results = []
    if not isinstance(news_results, list):
        news_results = []

    # Step 2: Key elements/leads
    leads = extract_leads(web_results + twitter_results + news_results)

    # Step 3: Research leads
    researched = research_leads(leads, topic)

    # Step 4: Collect data
    data = collect_data(researched)

    # Step 5: Analyze
    analysis = analyze(data)

    # Step 6: Conclusion
    conclusion = conclude(analysis)

    # Output JSON
    output = {
        "topic": topic,
        "gathered": {
            "web": web_results,
            "twitter": twitter_results,
            "news": news_results
        },
        "leads": leads,
        "researched": researched,
        "data": data,
        "analysis": analysis,
        "conclusion": conclusion
    }
    print(json.dumps(output, indent=2))

def gather_web(topic):
    with DDGS() as ddgs:
        results = [r for r in ddgs.text(topic, max_results=5)]
    return results

def gather_twitter(topic):
    bearer = os.environ.get('TWITTER_BEARER')
    if not bearer:
        return {"error": "No TWITTER_BEARER set"}
    url = f"https://api.twitter.com/2/tweets/search/recent?query={requests.utils.quote(topic)}&max_results=10"
    headers = {"Authorization": f"Bearer {bearer}"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return {"error": response.text}
    return response.json().get('data', [])

def gather_news(topic):
    api_key = os.environ.get('NEWS_API_KEY')
    if not api_key:
        return {"error": "No NEWS_API_KEY set"}
    url = f"https://newsapi.org/v2/everything?q={requests.utils.quote(topic)}&apiKey={api_key}"
    response = requests.get(url)
    if response.status_code != 200:
        return {"error": response.text}
    return response.json().get('articles', [])

def extract_leads(data):
    leads = set()
    for item in data:
        if isinstance(item, dict):
            if 'title' in item:
                leads.add(item['title'])
            if 'body' in item:
                leads.add(item['body'])  # For DDG
            if 'text' in item:  # For tweets
                leads.add(item['text'])
            if 'description' in item:
                leads.add(item['description'])
    return list(leads)

def research_leads(leads, topic):
    researched = {}
    for lead in leads[:3]:  # Limit to 3 to avoid too much
        researched[lead] = gather_web(lead + " " + topic)
    return researched

def collect_data(researched):
    # Simple flatten
    data = []
    for lead, results in researched.items():
        data.extend(results)
    return data

def analyze(data):
    # Mock analysis: count items, summary
    num_items = len(data)
    summary = f"Collected {num_items} data points. Key themes: investments, ETFs (assuming based on topic)."
    return summary

def conclude(analysis):
    return f"Based on {analysis}, situational awareness: The topic appears to be an ETF. Decision: Further research recommended before investing."

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 10-11.py <topic>")
        sys.exit(1)
    topic = sys.argv[1]
    main(topic)