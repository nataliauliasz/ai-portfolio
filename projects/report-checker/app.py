from flask import Flask, render_template, request, Response
import subprocess
import threading
import queue
import os
import time
import tempfile

app = Flask(__name__)

log_queue = queue.Queue()

# Funkcja uruchamiająca skrypt i przekierowująca stdout do kolejki
def run_analysis(pdf_path):
    print("▶️ Start analizy PDF:", pdf_path)
    process = subprocess.Popen(
        ['python', 'report_checking_program.py', pdf_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True,
        encoding='utf-8',
        errors='replace'
    )
    for line in process.stdout:
        log_queue.put(line)
    process.stdout.close()
    process.wait()
    log_queue.put('[KONIEC]')


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files['pdf']
    if file:
        save_path = os.path.join("uploads", file.filename)
        os.makedirs("uploads", exist_ok=True)
        file.save(save_path)
        threading.Thread(target=run_analysis, args=(save_path,), daemon=True).start()
        return "OK"
    return "Brak pliku", 400

@app.route('/stream')
def stream():
    def generate():
        while True:
            line = log_queue.get()
            yield f"data: {line}\n\n"
            if "[KONIEC]" in line:
                break
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5021, debug=True)
