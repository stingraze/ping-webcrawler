# (C)Tsubasa Kato - Inspire Search Corporation Last Updated: 2024/9/7 8:02AM JST
# Visit our company at: https://www.inspiresearch.io/en
# ping-webcrawler is a web crawler that first measures response time from the web servers.
# Created with the help of Perplexity Pro etc.
# Enjoy! Happy Crawling.
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify
import requests
import heapq
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
import csv
import signal
import sys
import threading
import time
import chardet
from urllib.parse import urljoin, urlparse

app = Flask(__name__)

extracted_data = []
data_lock = threading.Lock()
progress = []
crawling_done = threading.Event()
stop_crawling = threading.Event()

REQUEST_TIMEOUT = 5
MAX_DEPTH = 3
MAX_URLS = 100
MAX_CHARS = 800

def signal_handler(sig, frame):
    print("Interrupt received, saving data to CSV...")
    stop_crawling.set()
    save_to_csv_append()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

def measure_response_time(url):
    if stop_crawling.is_set():
        return (float('inf'), url)
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        progress.append(f"Measured response time for {url}: {response.elapsed.total_seconds()} seconds")
        return (response.elapsed.total_seconds(), url)
    except (requests.RequestException, TimeoutError):
        progress.append(f"Measurement timed out for {url}. Skipping...")
        return (float('inf'), url)

def extract_metadata_and_content(response):
    detected_encoding = chardet.detect(response.content).get('encoding', 'utf-8')
    response.encoding = detected_encoding

    soup = BeautifulSoup(response.content, 'html.parser', from_encoding=response.encoding)

    title = soup.title.string if soup.title else "N/A"
    keywords = soup.find("meta", {"name": "keywords"})
    keywords = keywords['content'] if keywords else "N/A"
    description = soup.find("meta", {"name": "description"})
    description = description['content'] if description else "N/A"
    body_content = soup.body.get_text(strip=True) if soup.body else "N/A"

    links = [urljoin(response.url, link.get('href')) for link in soup.find_all('a', href=True)]

    return title, keywords, description, body_content, links

def crawl_url(url, depth=0, visited=None):
    if visited is None:
        visited = set()

    if stop_crawling.is_set() or depth > MAX_DEPTH or url in visited or len(extracted_data) >= MAX_URLS:
        return

    visited.add(url)

    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT)
        progress.append(f"Crawling {url}: {response.status_code}")
        if response.status_code == 200:
            title, keywords, description, body_content, links = extract_metadata_and_content(response)
            with data_lock:
                extracted_data.append({
                    "URL": url[:MAX_CHARS],
                    "keywords": keywords[:MAX_CHARS],
                    "description": description[:MAX_CHARS],
                    "body content": body_content[:MAX_CHARS],
                    "title": title[:MAX_CHARS]
                })

            for link in links:
                if urlparse(link).netloc == urlparse(url).netloc:
                    crawl_url(link, depth + 1, visited)

    except (requests.RequestException, TimeoutError):
        progress.append(f"Crawling timed out for {url}. Skipping...")

def save_to_csv_append():
    with data_lock:
        with open('extracted_data.csv', 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['URL', 'keywords', 'description', 'body content', 'title']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if csvfile.tell() == 0:
                writer.writeheader()
            writer.writerows(extracted_data)
        print('Data appended to extracted_data.csv')
        extracted_data.clear()

def periodic_save_to_csv():
    while not crawling_done.is_set() and not stop_crawling.is_set():
        time.sleep(5)
        save_to_csv_append()

def optimized_crawl(urls):
    threading.Thread(target=periodic_save_to_csv, daemon=True).start()

    with ThreadPoolExecutor(max_workers=10) as executor:
        response_times = list(executor.map(measure_response_time, urls))
    
    valid_urls = [(time, url) for time, url in response_times if time != float('inf')]
    heapq.heapify(valid_urls)

    visited = set()
    for _, url in valid_urls:
        if stop_crawling.is_set():
            break
        if len(extracted_data) < MAX_URLS:
            crawl_url(url, visited=visited)
            time.sleep(0.5)
        else:
            break
    
    crawling_done.set()
    save_to_csv_append()

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        urls_input = request.form.get('urls')
        if urls_input:
            global extracted_data, progress, crawling_done, stop_crawling
            extracted_data = []
            progress = []
            crawling_done.clear()
            stop_crawling.clear()

            urls_to_crawl = [url.strip() for url in urls_input.splitlines() if url.strip()]

            threading.Thread(target=optimized_crawl, args=(urls_to_crawl,)).start()

            return redirect(url_for('index'))
    return render_template('index.html')

@app.route('/progress')
def progress_feed():
    def generate():
        while not crawling_done.is_set() and not stop_crawling.is_set():
            with data_lock:
                if progress:
                    yield f"data: {progress.pop(0)}\n\n"
            time.sleep(0.5)
        yield "data: Crawling finished\n\n"
    return Response(generate(), mimetype='text/event-stream')

@app.route('/stop', methods=['POST'])
def stop():
    stop_crawling.set()
    return jsonify({"status": "Crawling stopped"})

if __name__ == '__main__':
    app.run(debug=True)

