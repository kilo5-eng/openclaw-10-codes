#!/usr/bin/env python3
import argparse
import requests
import json
import sys

parser = argparse.ArgumentParser()
parser.add_argument('--host', default='localhost:3000')
parser.add_argument('--user', default='admin')
parser.add_argument('--password', default='admin')
parser.add_argument('action', choices=['list-dash', 'list-ds', 'summary', 'health', 'set-panel-query'])
args = parser.parse_args()

session = requests.Session()
session.auth = (args.user, args.password)
base = f'http://{args.host}/api'

if args.action == 'list-dash':
 r = session.get(f'{base}/dashboards/home')
 print(json.dumps(r.json(), indent=2))
elif args.action == 'list-ds':
 r = session.get(f'{base}/datasources')
 print(json.dumps(r.json(), indent=2))
elif args.action == 'health':
    r = session.get(f'{base}/health')
    print(r.json())
elif args.action == 'summary':
 r = session.get(f'{base}/dashboards/home')
 dash = r.json()
 r = session.get(f'{base}/datasources')
 ds = r.json()
 print(f'\\nDashboards: {len(dash["dashboard"]["panels"])} panels')
 print(f'Datasources: {len(ds["datasources"])}')
 print('\\nDash Titles:', [p.get('title', 'no title') for p in dash["dashboard"]["panels"] if p.get("title")])
 print('\\nDS:', [d['name'] for d in ds["datasources"]])