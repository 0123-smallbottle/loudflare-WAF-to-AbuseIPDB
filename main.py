import json
import requests
import time
import os
import yaml

def load_config(path):
    with open(path, 'r', encoding='utf-8') as f:
        ystr = f.read()
        ymllist = yaml.load(ystr, Loader=yaml.FullLoader)
        return ymllist

# Load configuration from file or environment variables
if os.path.exists('config.yml'):
    c = load_config('config.yml')
    CLOUDFLARE_ZONE_IDS = c['CLOUDFLARE_ZONE_IDS']
    CLOUDFLARE_EMAIL = c['CLOUDFLARE_EMAIL']
    CLOUDFLARE_API_KEY = c['CLOUDFLARE_API_KEY']
    ABUSEIPDB_API_KEY = c['ABUSEIPDB_API_KEY']
    WHITELISTED_IPS = c.get('WHITELISTED_IPS', "").split(",")
    DISCORD_WEBHOOK_URL = c.get('DISCORD_WEBHOOK_URL', '')
    REPORT_IPS = c.get('REPORT_IPS', 'true').lower() == 'true'
    SEND_DISCORD_WEBHOOK = c.get('SEND_DISCORD_WEBHOOK', 'true').lower() == 'true'
    ACTION = c.get('ACTION')
    CUSTOM_MESSAGE = c.get('CUSTOM_MESSAGE', '')

def get_blocked_ips(zone_id, max_retries=3):
    payload = {
        "query": """query ListFirewallEvents($zoneTag: string, $filter: FirewallEventsAdaptiveFilter_InputObject) {
            viewer {
                zones(filter: { zoneTag: $zoneTag }) {
                    firewallEventsAdaptive(
                        filter: $filter
                        limit: 1000
                        orderBy: [datetime_DESC]
                    ) {
                        action
                        clientASNDescription
                        clientAsn
                        clientCountryName
                        clientIP
                        clientRequestHTTPMethodName
                        clientRequestHTTPProtocol
                        clientRequestPath
                        clientRequestQuery
                        datetime
                        rayName
                        ruleId
                        source
                        userAgent
                    }
                }
            }
        }""",
        "variables": {
            "zoneTag": zone_id,
            "filter": {
                "datetime_geq": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(time.time() - 60*60*10.5)),
                "datetime_leq": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.localtime(time.time() - 60*60*8))
            }
        }
    }
    payload = json.dumps(payload)
    headers = {
        "Content-Type": "application/json",
        "X-Auth-Key": CLOUDFLARE_API_KEY,
        "X-Auth-Email": CLOUDFLARE_EMAIL
    }

    for attempt in range(max_retries):
        try:
            r = requests.post("https://api.cloudflare.com/client/v4/graphql/", headers=headers, data=payload, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"Attempt {attempt + 1}/{max_retries} failed: {str(e)}")
            if attempt == max_retries - 1:
                print("Max retries reached, giving up")
                return None
            time.sleep(2 ** attempt) 

def get_comment(it):
    return (f"The IP has triggered Cloudflare WAF. action: {it['action']} source: {it['source']} "
            f"clientAsn: {it['clientAsn']} clientASNDescription: {it['clientASNDescription']} "
            f"clientCountryName: {it['clientCountryName']} clientIP: {it['clientIP']} "
            f"clientRequestHTTPMethodName: {it['clientRequestHTTPMethodName']} "
            f"clientRequestHTTPProtocol: {it['clientRequestHTTPProtocol']} "
            f"clientRequestPath: {it['clientRequestPath']} "
            f"clientRequestQuery: {it['clientRequestQuery']} datetime: {it['datetime']} "
            f"rayName: {it['rayName']} ruleId: {it['ruleId']} userAgent: {it['userAgent']}. "
            f"{CUSTOM_MESSAGE}")

def get_country_flag_emoji(country_code):
    return "".join([chr(ord(c) + 127397) for c in country_code.upper()])

def send_discord_notification(events_batch, abuse_responses=None):
    if not DISCORD_WEBHOOK_URL or not SEND_DISCORD_WEBHOOK:
        return

    embeds = []
    if not REPORT_IPS:
        # Group all events for same IP into one embed
        ip_events = {}
        for event in events_batch:
            if event['clientIP'] not in ip_events:
                ip_events[event['clientIP']] = []
            ip_events[event['clientIP']].append(event)
        
        for ip, ip_event_list in ip_events.items():
            first_event = ip_event_list[0]
            country_flag = get_country_flag_emoji(first_event['clientCountryName'])
            
            fields = [
                {"name": "IP Address", "value": first_event['clientIP'], "inline": True},
                {"name": "Country", "value": f"{country_flag} {first_event['clientCountryName']}", "inline": True},
                {"name": "ASN", "value": f"{first_event['clientAsn']} ({first_event['clientASNDescription']})", "inline": False},
                {"name": "Total Events", "value": str(len(ip_event_list)), "inline": True},
            ]

            # Add details of each event
            event_details = []
            for idx, event in enumerate(ip_event_list, 1):
                event_details.append(f"Event {idx}:")
                event_details.append(f"Action: {event['action']}")
                event_details.append(f"Source: {event['source']}")
                event_details.append(f"Path: {event['clientRequestPath'][:256]}")
                event_details.append(f"Time: {event['datetime']}\n")

            fields.append({"name": "Event Details", "value": "\n".join(event_details)[:1024], "inline": False})

            embeds.append({
                "title": "WAF Events Detected",
                "color": 0xFF0000,
                "fields": fields,
                "timestamp": first_event['datetime']
            })
    else:
        # Create individual embeds for report mode
        for i, event in enumerate(events_batch):
            country_flag = get_country_flag_emoji(event['clientCountryName'])
            fields = [
                {"name": "IP Address", "value": event['clientIP'], "inline": True},
                {"name": "Country", "value": f"{country_flag} {event['clientCountryName']}", "inline": True},
                {"name": "ASN", "value": f"{event['clientAsn']} ({event['clientASNDescription']})", "inline": False},
                {"name": "Action", "value": event['action'], "inline": True},
                {"name": "Source", "value": event['source'], "inline": True},
                {"name": "Method", "value": event['clientRequestHTTPMethodName'], "inline": True},
                {"name": "Path", "value": event['clientRequestPath'][:1024], "inline": False},
            ]

            if abuse_responses and len(abuse_responses) > i and abuse_responses[i] and 'data' in abuse_responses[i]:
                report_data = abuse_responses[i]['data']
                fields.append({"name": "AbuseIPDB Report", "value": f"Report #{report_data.get('reportNumber')} - Confidence: {report_data.get('abuseConfidenceScore')}%", "inline": False})

            embeds.append({
                "title": "WAF Event Reported to AbuseIPDB",
                "color": 0xFF0000,
                "fields": fields,
                "timestamp": event['datetime']
            })

    # Send embeds in groups of 10   
    for i in range(0, len(embeds), 10):
        chunk = embeds[i:i+10]
        payload = {"embeds": chunk}
        try:
            requests.post(DISCORD_WEBHOOK_URL, json=payload)
        except Exception as e:
            print(f"Failed to send Discord notification: {str(e)}")

def report_bad_ip(it):
    if not REPORT_IPS:
        print(f"Skipping report for IP {it['clientIP']} (reporting disabled)")
        return None
        
    try:
        url = 'https://api.abuseipdb.com/api/v2/report'
        params = {
            'ip': it['clientIP'],
            'categories': '10,19',
            'comment': get_comment(it)
        }
        headers = {
            'Accept': 'application/json',
            'Key': ABUSEIPDB_API_KEY
        }
        r = requests.post(url=url, headers=headers, params=params)
        if r.status_code == 200:
            print(f"Reported: {it['clientIP']}")
            response_data = r.json()
            print(json.dumps(response_data, sort_keys=True, indent=4))
            return response_data
        else:
            print(f"Error: HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"Error reporting IP: {str(e)}")
        return None

def main():
    print("==================== Start ====================")
    print(f"Current time: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
    print(f"Query time range: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time() - 60*60*8))}")
    
    reported_ip_list = []
    events_batch = []
    abuse_responses = []

    for zone_id in CLOUDFLARE_ZONE_IDS:
        print(f"Processing Zone ID: {zone_id}")
        response = get_blocked_ips(zone_id)
        if not response or "data" not in response or "viewer" not in response["data"]:
            print(f"Failed to get blocked IPs for Zone ID: {zone_id}")
            continue

        ip_bad_list = response["data"]["viewer"]["zones"][0]["firewallEventsAdaptive"]
        print(f"Total events found in Zone {zone_id}: {len(ip_bad_list)}")

        for event in ip_bad_list:
            if (event['clientIP'] not in reported_ip_list and 
                event['clientIP'] not in WHITELISTED_IPS and
                event['action'] == ACTION):
                print(f"IP: {event['clientIP']}, Location: {event['clientCountryName']}, Time: {event['datetime']}")
                
                events_batch.append(event)
                if REPORT_IPS:
                    abuse_response = report_bad_ip(event)
                    abuse_responses.append(abuse_response)
                    if abuse_response:
                        reported_ip_list.append(event['clientIP'])

                # Send in batches of 10
                if len(events_batch) >= 10:
                    send_discord_notification(events_batch, abuse_responses if REPORT_IPS else None)
                    events_batch = []
                    abuse_responses = []

    # Send remaining events
    if events_batch:
        send_discord_notification(events_batch, abuse_responses if REPORT_IPS else None)

    print(f"Total unique IPs reported: {len(reported_ip_list)}")
    print("==================== End ====================")

def run_loop():
    while True:
        main()
        time.sleep(3600)

if __name__ == "__main__":
    run_loop()