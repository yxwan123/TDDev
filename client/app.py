from flask import Flask, render_template, request, jsonify
import requests
import json
import asyncio
import zipfile
import os
import subprocess
import time
import re
from pathlib import Path
import base64
import csv
from datetime import datetime
from playwright.sync_api import sync_playwright
from dotenv import dotenv_values
from browser_use.llm import ChatAnthropic
from browser_use import Agent, BrowserSession
from bots import OpenAILLM
from prompts import get_prompt

# Load only selected keys from bolt.diy/.env.local if present
ENV_PATH = Path(__file__).resolve().parents[1] / "bolt.diy" / ".env.local"
if ENV_PATH.exists():
    env_map = dotenv_values(ENV_PATH)
    for key_name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "TOGETHER_API_KEY"):
        value = env_map.get(key_name)
        if value and not os.getenv(key_name):
            os.environ[key_name] = value

PARALLEL_AGENT_COUNT = int(os.environ.get("PARALLEL_AGENT_COUNT", "3"))
round_limit = 6
agent_execution_status = {
    "is_running": False,
    "total_agents": PARALLEL_AGENT_COUNT,
    "start_time": None,
    "end_time": None,
    "current_results": []
}
last_called_route = "textgen"
image = ""
model = "gpt-4.1"
base_url = None
key = os.getenv("OPENAI_API_KEY")
provider = "OpenAI"
global_test_criteria = None
compare_result = None
vali_run_counter = 0
csv_file_path = None
max_wait_time = 2 * 60 * 60
id = "000"
browser_session = BrowserSession(executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
llm = ChatAnthropic(
    model="claude-sonnet-4-20250514",
)

app = Flask(__name__)

DETECTION_TIMEOUT = 60
PM2_LOG_DIR = os.path.expanduser("~/.pm2/logs")

def _reset():
    global agent_execution_status, image, global_test_criteria, compare_result, vali_run_counter, csv_file_path
    agent_execution_status = {
        "is_running": False,
        "total_agents": PARALLEL_AGENT_COUNT,
        "start_time": None,
        "end_time": None,
        "current_results": []
    }
    image = ""
    global_test_criteria = None
    compare_result = None
    vali_run_counter = 0
    csv_file_path = None


def _remove_files_in_dir(dir_path: str) -> None:
    if not os.path.isdir(dir_path):
        return
    for entry in os.listdir(dir_path):
        fp = os.path.join(dir_path, entry)
        if os.path.isfile(fp):
            try:
                os.remove(fp)
            except Exception:
                pass

def _run_cmd(cmd: str, cwd: str | None = None, check: bool = False, capture: bool = False, timeout: int = 300):
    kwargs = {"shell": True, "cwd": cwd, "timeout": timeout}
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True})
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout if capture else None, proc.stderr if capture else None)
    return proc


def _read_package_script_name(app_dir: str) -> str:
    pkg = Path(app_dir) / "package.json"
    if pkg.is_file():
        try:
            with open(pkg, "r", encoding="utf-8") as f:
                data = json.load(f)
            scripts = data.get("scripts", {}) or {}
            if "dev" in scripts:
                return "dev"
            if "start" in scripts:
                return "start"
        except Exception:
            pass
    return "dev"


def _write_ecosystem_for_instances(app_dir: str, num_instances: int, script_name: str) -> str:
    apps = []
    for i in range(num_instances):
        app_name = f"webapp-{i+1}"
        # Ensure the path is properly escaped for JSON
        safe_app_dir = app_dir.replace("\\", "/")
        apps.append({
            "name": app_name,
            "cwd": safe_app_dir,
            "script": "npm",
            "args": f"run {script_name}",
            "env": {}
        })
    config = {"apps": apps}
    ecosystem_path = os.path.join(app_dir, "ecosystem.config.cjs")
    content = "module.exports = " + json.dumps(config, indent=2) + ";"
    with open(ecosystem_path, "w", encoding="utf-8") as f:
        f.write(content)
    return ecosystem_path


def _pm2_start(ecosystem_file: str, app_dir: str, app_names: list[str]):
    for name in app_names:
        _run_cmd(f"pm2 delete {name}", cwd=app_dir, capture=True)
    # Use quotes around ecosystem_file path to handle special characters
    _run_cmd(f'pm2 start "{ecosystem_file}"', cwd=app_dir, check=True)


def _detect_ports_from_pm2_logs(app_names: list[str], timeout: int = DETECTION_TIMEOUT) -> list[int]:
    results: dict[str, int] = {}
    port_pattern = re.compile(r"http[s]?://(?:localhost|127\.0\.0\.1):(\d+)", re.IGNORECASE)
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    start_time = time.time()
    while time.time() - start_time < timeout:
        for name in app_names:
            if name in results:
                continue
            log_files = [
                os.path.join(PM2_LOG_DIR, f"{name}-out.log"),
                os.path.join(PM2_LOG_DIR, f"{name}-error.log"),
            ]
            for log_file in log_files:
                if not os.path.exists(log_file):
                    continue
                try:
                    with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                        content = ansi_escape.sub('', content).strip()
                        match = port_pattern.search(content)
                        if match:
                            results[name] = int(match.group(1))
                            break
                except Exception:
                    pass
        if len(results) == len(app_names):
            break
        time.sleep(0.8)

    return [results[name] for name in app_names if name in results]


def start_multiple_webapps_pm2(num_instances: int, app_dir: str) -> list[int]:
    """Run multiple web applications with pm2
    npm install -> make ecosystem.config.js -> pm2 start -> extract ports from logs
    """
    app_dir = str(app_dir)
    Path(app_dir).mkdir(parents=True, exist_ok=True)

    try:
        _run_cmd("npm install", cwd=app_dir, check=True, timeout=600)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
        try:
            _run_cmd("npm install --force", cwd=app_dir, check=True, timeout=600)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            _run_cmd("npm install --legacy-peer-deps", cwd=app_dir, check=True, timeout=600)

    _remove_files_in_dir(PM2_LOG_DIR)

    script_name = _read_package_script_name(app_dir)
    ecosystem_path = _write_ecosystem_for_instances(app_dir, num_instances, script_name)

    app_names = [f"webapp-{i+1}" for i in range(num_instances)]
    _pm2_start(ecosystem_path, app_dir, app_names)

    ports = _detect_ports_from_pm2_logs(app_names, timeout=DETECTION_TIMEOUT)
    print(f"PM2 ports: {ports}")
    return ports


def _pm2_list_names() -> list[str]:
    try:
        proc = _run_cmd("pm2 jlist", capture=True)
        if proc.returncode != 0 or not proc.stdout:
            return []
        data = json.loads(proc.stdout)
        return [item.get("name", "") for item in data if item.get("name")]
    except Exception:
        return []


def stop_all_webapps_pm2(prefix: str = "webapp-") -> list[str]:
    names = [n for n in _pm2_list_names() if isinstance(n, str) and n.startswith(prefix)]
    for name in names:
        try:
            _run_cmd(f"pm2 delete {name}")
        except Exception:
            pass
    return names

def read_json_as_string(file_path='req.json'):
    if os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return json.dumps(data, ensure_ascii=False)
        except Exception as e:
            return str(e)
    return None

def validate_json_string(json_str):
    try:
        parsed_json = json.loads(json_str)
        return True, parsed_json, None
    except json.JSONDecodeError as e:
        error_msg = {str(e)}
        print(error_msg)
        return False, None, error_msg

def save_json_if_absent(data, file_path='req.json'):
    if not os.path.exists(file_path):
        print(f"Writing JSON file {file_path}...")
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4)

def update_csv_results(round_num, folder_name, success_count, fail_count):
    global csv_file_path
    if csv_file_path and csv_file_path.exists():
        try:
            rate = success_count / (success_count + fail_count) * 100 if (success_count + fail_count) > 0 else 0
            with open(csv_file_path, 'a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow([round_num, folder_name, success_count, fail_count, f"{rate:.2f}%"])
            print(f"CSV updated: Round {round_num}, Success: {success_count}, Fail: {fail_count}, Rate: {rate:.2f}%")
        except Exception as e:
            print(f"Error when updating the csv {str(e)}")


def capture_screenshot_as_base64(url, save_path=None):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})
            page.goto(url, wait_until="networkidle")
            page.wait_for_load_state("networkidle")
            screenshot_bytes = page.screenshot(full_page=True, type="png")

            if save_path:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(screenshot_bytes)

            browser.close()
            base64_string = base64.b64encode(screenshot_bytes).decode('utf-8')

            return base64_string

    except Exception as e:
        print(f"Fail to capture a screenshot: {str(e)}")
        return None


@app.route('/')
def index():
    return render_template('tool.html')

@app.route('/config', methods=['GET', 'POST'])
def config():
    """Configure parallel count, round limit and max wait time parameters"""
    global PARALLEL_AGENT_COUNT, round_limit, vali_run_counter, max_wait_time
    
    if request.method == 'POST':
        data = request.json
        new_count = data.get('parallel_count')
        new_round_limit = data.get('round_limit')
        new_max_wait_time = data.get('max_wait_time')
        
        # Update parallel count if provided
        if new_count is not None:
            if isinstance(new_count, int) and new_count > 0:
                PARALLEL_AGENT_COUNT = new_count
            else:
                return jsonify({
                    "success": False, 
                    "message": "Invalid parallel count, must be a positive integer"
                })
        
        # Update round limit if provided
        if new_round_limit is not None:
            if isinstance(new_round_limit, int) and new_round_limit > 0:
                round_limit = new_round_limit
            else:
                return jsonify({
                    "success": False, 
                    "message": "Invalid round limit, must be a positive integer"
                })
        
        # Update max wait time if provided
        if new_max_wait_time is not None:
            if isinstance(new_max_wait_time, int) and new_max_wait_time > 0:
                max_wait_time = new_max_wait_time
            else:
                return jsonify({
                    "success": False, 
                    "message": "Invalid max wait time, must be a positive integer"
                })
        
        return jsonify({
            "success": True, 
            "message": f"Configuration updated successfully",
            "current_parallel_count": PARALLEL_AGENT_COUNT,
            "current_round_limit": round_limit,
            "current_max_wait_time": max_wait_time,
            "current_round_counter": vali_run_counter
        })
    
    # GET request returns current configuration
    return jsonify({
        "current_parallel_count": PARALLEL_AGENT_COUNT,
        "current_round_limit": round_limit,
        "current_max_wait_time": max_wait_time,
        "current_round_counter": vali_run_counter,
        "message": f"Configuration loaded successfully。"
    })

@app.route('/status', methods=['GET'])
def get_status():
    """Get agent status"""
    global agent_execution_status, round_limit, vali_run_counter

    execution_time = None
    if agent_execution_status["start_time"] and agent_execution_status["end_time"]:
        execution_time = round(agent_execution_status["end_time"] - agent_execution_status["start_time"], 2)
    elif agent_execution_status["start_time"] and agent_execution_status["is_running"]:
        execution_time = round(time.time() - agent_execution_status["start_time"], 2)
    
    status_info = {
        "current_val_round": vali_run_counter,
        "val_round_limit": round_limit,
        **agent_execution_status,
        "execution_time_seconds": execution_time,
        "parallel_count": PARALLEL_AGENT_COUNT
    }
    
    return jsonify(status_info)

@app.route('/cache', methods=['GET'])
def get_cache():
    """Get cached prompt data"""
    result = {
        "model": "",
        "data": ""
    }

    try:
        if os.path.exists('direct_prompt.txt'):
            with open('direct_prompt.txt', 'r', encoding='utf-8') as f:
                result["data"] = f.read()
            result["model"] = ""
        else:
            pattern = r'(\w+)_prompt_(\w+)\.txt'
            matching_files = []
            for filename in os.listdir('.'):
                match = re.match(pattern, filename)
                if match:
                    file_id, model_name = match.groups()
                    matching_files.append((filename, file_id, model_name))

            if matching_files:
                filename, file_id, model_name = matching_files[0]
                with open(filename, 'r', encoding='utf-8') as f:
                    result["data"] = f.read()
                result["model"] = model_name
            else:
                result["model"] = ""
                result["data"] = ""
            
    except Exception as e:
        return jsonify({
            "error": f"Error: {str(e)}",
            "model": "",
            "data": ""
        })
    
    return jsonify(result)


@app.route('/clear', methods=['GET'])
def clear_files():
    try:
        current_dir = os.getcwd()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        cache_dir = os.path.join(current_dir, "cache", timestamp)

        os.makedirs(cache_dir, exist_ok=True)

        moved_files = []
        error_files = []

        for filename in os.listdir(current_dir):
            if filename.endswith(('.txt', '.json')):
                source_path = os.path.join(current_dir, filename)
                destination_path = os.path.join(cache_dir, filename)

                try:
                    os.rename(source_path, destination_path)
                    moved_files.append(filename)
                    print(f"Moved: {filename} -> {destination_path}")
                except Exception as e:
                    error_files.append(f"{filename}: {str(e)}")
                    print(f"Fail to move: {filename} - {str(e)}")

        result = {
            "success": True,
            "message": f"Success to move {len(moved_files)} files.",
            "cache_directory": cache_dir,
            "moved_files": moved_files,
            "error_files": error_files,
            "total_moved": len(moved_files),
            "total_errors": len(error_files)
        }

        if error_files:
            result["message"] += f"，{len(error_files)} files move failed."

        return jsonify(result)

    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"Error: {str(e)}",
            "error": str(e)
        })

@app.route('/textgenv1', methods=['POST'])
def textgenv1():
    global last_called_route, model, base_url, key, provider, image
    last_called_route = "textgenv1"
    
    data = request.json
    prompt = data.get('prompt')
    model = data.get('model')
    request_image = data.get('image')
    image = request_image
    if model == "openai":
        model = "gpt-4.1"
        base_url = None
        key = os.getenv("OPENAI_API_KEY")
        provider = "OpenAI"
    elif model == "claude":
        model = "claude-sonnet-4-20250514"
        base_url = "https://api.anthropic.com/v1/"
        key = os.getenv("ANTHROPIC_API_KEY")
        provider = "Anthropic"
    elif model == "qwen":
        model = "Qwen/Qwen2.5-VL-72B-Instruct"
        base_url = "https://api.together.xyz/v1"
        key = os.getenv("TOGETHER_API_KEY")
        provider = "Together"
    elif model == "deepseek":
        model = "deepseek-ai/DeepSeek-V3.1"
        base_url = "https://api.together.xyz/v1"
        key = os.getenv("TOGETHER_API_KEY")
        provider = "Together"

    if os.path.exists('req.json') and os.path.exists('direct_prompt.txt'):
        try:
            with open('req.json', 'r', encoding='utf-8') as f:
                response_data = json.load(f)
            response = json.dumps(response_data, ensure_ascii=False)
            with open('direct_prompt.txt', 'r', encoding='utf-8') as f:
                prompt = f.read()
            print("Use caches")
        except Exception as e:
            response = str(e)
    else:
        bot = OpenAILLM(key, base_url=base_url, model=model)
        try:
            response = bot.ask(get_prompt("REQUIREMENT", instruction=str(prompt)), image_encoding=image, verbose=True)
        except Exception as e:
            response = str(e)

    global global_test_criteria
    global_test_criteria = []
    try:
        response_data = json.loads(response)

        if not os.path.exists('req.json'):
            file_name = 'req.json'
            with open(file_name, 'w', encoding='utf-8') as f:
                json.dump(response_data, f, ensure_ascii=False, indent=4)

        if not os.path.exists('direct_prompt.txt'):
            with open('direct_prompt.txt', "w", encoding="utf-8") as f:
                f.write(prompt)

        for item in response_data:
            new_item = {"static_description": item["static_description"], "test_criteria": item["test_criteria"]}
            del item["test_criteria"]
            global_test_criteria.append(new_item)

        processed_response = json.dumps(response_data, ensure_ascii=False)
    except json.JSONDecodeError:
        processed_response = response

    result_json = {
        "model": model,
        "provider": {
            "name": provider
        },
        "input": get_prompt("WEB_GENERATE", instruction=str(prompt), requirement_list=str(processed_response)),
        "imageDataList": [f"data:image/png;base64,{image}"]
    }

    try:
        external_response = requests.post(
            "http://localhost:5173/api/external-send",
            json=result_json,
            headers={"Content-Type": "application/json"}
        )
        external_response.raise_for_status()
        return jsonify({"success": True, "response": response})
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": str(e), "response": response, "data": result_json})



@app.route('/textgen', methods=['POST'])
def textgen():
    global last_called_route
    last_called_route = "textgen"

    data = request.json
    prompt = data.get('prompt')
    request_image = data.get('image')
    request_model = data.get('model')
    return direct_textgen(selected_model=request_model, prompt=prompt, image_encoding=request_image)


def direct_textgen(selected_model, prompt, image_encoding=None):
    global model, base_url, key, provider, image, id
    image = image_encoding

    if selected_model == "openai":
        model = "gpt-4.1"
        base_url = None
        key = os.getenv("OPENAI_API_KEY")
        provider = "OpenAI"
    elif selected_model == "claude":
        model = "claude-sonnet-4-20250514"
        base_url = "https://api.anthropic.com/v1/"
        key = os.getenv("ANTHROPIC_API_KEY")
        provider = "Anthropic"
    elif selected_model == "qwen":
        model = "Qwen/Qwen2.5-VL-72B-Instruct"
        base_url = "https://api.together.xyz/v1"
        key = os.getenv("TOGETHER_API_KEY")
        provider = "Together"
    elif selected_model == "deepseek":
        model = "deepseek-ai/DeepSeek-V3.1"
        base_url = "https://api.together.xyz/v1"
        key = os.getenv("TOGETHER_API_KEY")
        provider = "Together"
        image = ""
    bot = OpenAILLM(key, base_url=base_url, model=model)

    if os.path.exists(f'{id}_prompt_{selected_model}.txt'):
         with open(f'{id}_prompt_{selected_model}.txt', 'r', encoding='utf-8') as f:
            prompt = f.read()
            print("Use cached prompt")
    else:
        with open(f'{id}_prompt_{selected_model}.txt', "w", encoding="utf-8") as f:
            f.write(prompt)

    cached_requirements = read_json_as_string(f'{id}_requirements_{selected_model}.json')
    if cached_requirements is not None:
        print("Use cached requirements")
        requirements = cached_requirements
    else:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if image:
                    requirements = bot.ask(get_prompt("REQUIREMENT_DIVIDER_IMG", instruction=str(prompt)), image_encoding=image, verbose=True)
                else:
                    requirements = bot.ask(get_prompt("REQUIREMENT_DIVIDER", instruction=str(prompt)), verbose=True)
                
                is_valid, parsed_requirements, error_msg = validate_json_string(requirements)
                if is_valid:
                    print(f"Requirements JSON validation successful (attempt {attempt + 1}/{max_retries})")
                    save_json_if_absent(parsed_requirements, f'{id}_requirements_{selected_model}.json')
                    requirements = json.dumps(parsed_requirements, ensure_ascii=False)
                    break
                else:
                    print(f"Requirements JSON validation failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                    if attempt == max_retries - 1:
                        return f"Requirements JSON validation failed, maximum retry attempts reached: {error_msg}"
                    
            except Exception as e:
                if attempt == max_retries - 1:
                    return f"Requirements generation failed: {str(e)}"
                print(f"Requirements generation exception (attempt {attempt + 1}/{max_retries}): {str(e)}")

    if selected_model != "deepseek":
        cached_requirement_list = read_json_as_string(f'{id}_requirement_list_{selected_model}.json')
        if cached_requirement_list is not None:
            requirement_list = cached_requirement_list
        else:
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    if image:
                        requirement_list = bot.ask(get_prompt("REQUIREMENT_LIST_IMG", instruction=str(prompt), requirements=str(requirements)), image_encoding=image, verbose=True)
                    else:
                        requirement_list = bot.ask(get_prompt("REQUIREMENT_LIST", instruction=str(prompt), requirements=str(requirements)), verbose=True)

                    is_valid, parsed_requirement_list, error_msg = validate_json_string(requirement_list)
                    if is_valid:
                        print(f"Requirement list JSON validation successful (attempt {attempt + 1}/{max_retries})")
                        save_json_if_absent(parsed_requirement_list, f'{id}_requirement_list_{selected_model}.json')
                        requirement_list = json.dumps(parsed_requirement_list, ensure_ascii=False)
                        break
                    else:
                        print(f"Requirement list JSON validation failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                        if attempt == max_retries - 1:
                            return f"Requirement list JSON validation failed, maximum retry attempts reached: {error_msg}"

                except Exception as e:
                    if attempt == max_retries - 1:
                        return f"Requirement list generation failed: {str(e)}"
                    print(f"Requirement list generation exception (attempt {attempt + 1}/{max_retries}): {str(e)}")
    else:
        requirement_list = ""

    global global_test_criteria
    cached_test_criteria = read_json_as_string(f'{id}_test_criteria_{selected_model}.json')
    if cached_test_criteria is not None:
        cached_test_criteria = json.loads(cached_test_criteria)
        global_test_criteria = cached_test_criteria
    else:
        max_retries = 3
        for attempt in range(max_retries):
            try:
                if image:
                    global_test_criteria = bot.ask(get_prompt("TEST_CRITERIA_IMG", instruction=str(prompt), requirements=str(requirements), requirement_list=str(requirement_list)), image_encoding=image, verbose=True)
                if selected_model == "deepseek":
                    global_test_criteria = bot.ask(get_prompt("TEST_CRITERIA_DEEPSEEK", instruction=str(prompt), requirements=str(requirements)), verbose=True)
                else:
                    global_test_criteria = bot.ask(get_prompt("TEST_CRITERIA", instruction=str(prompt), requirements=str(requirements), requirement_list=str(requirement_list)), verbose=True)
                
                is_valid, parsed_test_criteria, error_msg = validate_json_string(global_test_criteria)
                if is_valid:
                    print(f"Test criteria JSON validation successful (attempt {attempt + 1}/{max_retries})")
                    save_json_if_absent(parsed_test_criteria, f'{id}_test_criteria_{selected_model}.json')
                    global_test_criteria = parsed_test_criteria
                    break
                else:
                    print(f"Test criteria JSON validation failed (attempt {attempt + 1}/{max_retries}): {error_msg}")
                    if attempt == max_retries - 1:
                        return f"Test criteria JSON validation failed, maximum retry attempts reached: {error_msg}"
                    
            except Exception as e:
                if attempt == max_retries - 1:
                    return f"Test criteria generation failed: {str(e)}"
                print(f"Test criteria generation exception (attempt {attempt + 1}/{max_retries}): {str(e)}")

    response = str(requirements) + str(requirement_list) + str(global_test_criteria)

    if image:
        result_json = {
            "model": model,
            "provider": {
                "name": provider
            },
            "input": get_prompt("WEB_GENERATE_MUL_IMG", instruction=str(prompt), requirements=str(requirements)),
            "imageDataList": [f"data:image/png;base64,{image}"]
        }
    else:
        result_json = {
            "model": model,
            "provider": {
                "name": provider
            },
            "input": get_prompt("WEB_GENERATE_MUL", instruction=str(prompt), requirements=str(requirements)),
            "imageDataList": []
        }

    try:
        external_response = requests.post(
            "http://localhost:5173/api/external-send",
            json=result_json,
            headers={"Content-Type": "application/json"}
        )
        external_response.raise_for_status()
        return jsonify({"success": True, "response": response})
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": str(e), "response": response, "data": result_json})




@app.route('/vali', methods=['GET'])
def vali():
    time.sleep(16)
    global last_called_route, vali_run_counter, csv_file_path, round_limit, id
    
    vali_run_counter += 1
    print(f"{vali_run_counter} validation")
    
    if vali_run_counter == 1:
        try:
            downloads_path = Path.home() / "Downloads"
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            csv_file_path = downloads_path / f"{id}_vali_results_{timestamp}.csv"
            
            with open(csv_file_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(['round', 'folder', 'success', 'fail', 'rate'])
            
            print(f"CSV file created: {csv_file_path}")
        except Exception as e:
            print(f"Error creating the csv file: {str(e)}")

    if vali_run_counter >= round_limit:
        return jsonify({"message": "success", "result": f"Reached {round_limit} rounds, stopping further rounds."})

    if round_limit - vali_run_counter <= 3:
        print(f"‼️‼️‼️‼️‼️‼️\nAttention: Only {round_limit - vali_run_counter} rounds left before reaching the limit of {round_limit} rounds.\n‼️‼️‼️‼️‼️‼️")
    
    if last_called_route == "textgenv1":
        return valiv1()
    else:
        return valiv2()

def valiv2():
    global id
    try:
        print("Validating...")
        file_name = request.args.get('fileName')
        if not file_name:
            return jsonify({"message": "error", "result": "fileName NOT found in the request"})
        
        import urllib.parse
        file_name = urllib.parse.unquote(file_name)

        downloads_path = Path.home() / "Downloads"
        zip_file_path = downloads_path / file_name

        if not zip_file_path.exists():
            return jsonify({"message": "error", "result": f" {file_name} non-exist in {zip_file_path}"})

        extract_folder_name = file_name.replace('.zip', '')
        extract_folder_name = extract_folder_name.replace('&', '_and_').replace(' ', '_')
        extract_path = downloads_path / extract_folder_name

        try:
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
        except Exception as e:
            return jsonify({"message": "error", "result": f"Fail to unzip: {str(e)}"})

        global global_test_criteria, compare_result, image, model, base_url, key, provider
        compare_result=None

        test_cases = global_test_criteria
        test_array = [json.dumps(item) for item in test_cases]
        total_test_count = len(test_array)
        
        print(f"Performing {total_test_count} test cases")
        try:
            os.chdir(extract_path)
            count = PARALLEL_AGENT_COUNT

            app_names = [f"webapp-{i+1}" for i in range(count)]
            

            try:
                ports = start_multiple_webapps_pm2(count, str(extract_path))
                if not ports:
                    return jsonify({"message": "error", "result": "PORT not found, check logs ~/.pm2/logs"})
            except Exception as e:
                return jsonify({"message": "continue", "result": get_prompt("LOADING_FAILED", detail=str(e)), "model": model, "provider": provider})

            print(f"{len(ports)} applications running, ports: {ports}")

            screenshot_name = file_name.replace('.zip', '.png')
            screenshot_path = downloads_path / screenshot_name
            screenshot_base64 = capture_screenshot_as_base64(f"http://localhost:{ports[0]}", str(screenshot_path))



            if screenshot_base64:
                bot = OpenAILLM(key=os.getenv("ANTHROPIC_API_KEY"), base_url="https://api.anthropic.com/v1/", model="claude-sonnet-4-20250514")
                try:
                    if image:
                        response = bot.ask(get_prompt("SCREENSHOT_IMG"), screenshot_base64, image, True)
                    else:
                        response = bot.ask(get_prompt("SCREENSHOT"), image_encoding=screenshot_base64, verbose=True)
                    data = json.loads(response)
                    if data.get("loading_success") == "True":
                        if image and data.get("detail"):
                            compare_result = data.get("detail")
                            print("Some differences found in screenshot comparison.")
                        else:
                            print("Page loaded correctly, proceeding with tests...")
                    elif data.get("loading_success") == "False":
                        detail = data.get("detail")
                        stop_all_webapps_pm2()
                        return jsonify({"message": "continue", "result": get_prompt("LOADING_FAILED", detail=detail), "model": model, "provider": provider})
                    else:
                        raise Exception("Error: Invalid value for 'loading_success' key.")
                except (json.JSONDecodeError, AttributeError, Exception) as e:
                    stop_all_webapps_pm2()
                    return jsonify({"message": "error", "result": f"Initial image validation failed: {str(e)}"})
            else:
                response = "Fail to capture screenshot"
                stop_all_webapps_pm2()
                return jsonify({"message": "continue", "result": get_prompt("LOADING_FAILED", detail=response), "model": model, "provider": provider})

            global browser_session, agent_execution_status

            agent_execution_status.update({
                "is_running": True,
                "total_tests": total_test_count,
                "completed_tests": 0,
                "successful_tests": 0,
                "failed_tests": 0,
                "start_time": time.time(),
                "end_time": None,
                "current_results": [],
                "current_round": 0
            })

            async def run_single_agent(agent_id: int, test_criteria: str, target_url: str):
                global model
                individual_browser_session = None
                try:
                    individual_browser_session = BrowserSession(
                        executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
                    )
                    
                    individual_llm = ChatAnthropic(
                        model="claude-sonnet-4-20250514",
                    )

                    if model == "deepseek-ai/DeepSeek-V3.1":
                        task = get_prompt(
                            "WEB_SOAP_TEST_DEEPSEEK",
                            url=target_url,
                            criteria=test_criteria
                        )
                    else:
                        task = get_prompt(
                            "WEB_SOAP_TEST",
                            url=target_url,
                            criteria=test_criteria
                        )

                    agent = Agent(
                        task=task,
                        llm=individual_llm,
                        browser_session=individual_browser_session,
                    )
                    
                    print(f"Agent {agent_id} running...")
                    result = await agent.run(max_steps=20)
                    final_result = result.final_result()
                    print(f"Agent {agent_id} completed: {final_result}")
                    
                    log_filename = f"log/browser_use_log_agent_{agent_id}_round_{agent_execution_status['current_round']}"
                    result.save_to_file(log_filename)
                    
                    return final_result

                except Exception as e:
                    print(f"Agent {agent_id} ERROR: {str(e)}")
                    return f"Error: {str(e)}"

                finally:
                    try:
                        if individual_browser_session is not None:
                            await individual_browser_session.close()
                    except Exception:
                        pass

            async def run_test_rounds():
                global agent_execution_status, provider, model
                completed_tests = 0
                successful_tests = 0
                failed_tests = 0
                all_results = []
                current_test_index = 0
                round_number = 0

                print(f"\n=== STARTING TESTING ===")
                current_round_tests = PARALLEL_AGENT_COUNT

                round_urls = []
                round_tasks = """You don't need to run any test or do anything, just skip and return a "Success" quickly."""

                for i in range(current_round_tests):
                    target_url = f"http://localhost:{ports[i % len(ports)]}"
                    round_urls.append(target_url)

                    print(f"Test cases {i + 1} -> {target_url}")

                tasks = []
                for i in range(current_round_tests):
                    task = run_single_agent(i + 1, round_tasks, round_urls[i])
                    tasks.append(task)

                print(f"Paralleling {len(tasks)} paddings...")
                await asyncio.gather(*tasks, return_exceptions=True)
                print("All paddings done.")

                
                while current_test_index < total_test_count:
                    round_number += 1
                    agent_execution_status['current_round'] = round_number
                    
                    remaining_tests = total_test_count - current_test_index
                    current_round_tests = min(PARALLEL_AGENT_COUNT, remaining_tests)
                    
                    print(f"\n=== {round_number} round started ===")
                    print(f"Running test cases: {current_round_tests}")
                    print(f"Remaining test cases: {remaining_tests}")
                    
                    round_tasks = []
                    round_urls = []
                    
                    for i in range(current_round_tests):
                        test_index = current_test_index + i
                        test_criteria = test_array[test_index]
                        target_url = f"http://localhost:{ports[i % len(ports)]}"
                        
                        round_tasks.append(test_criteria)
                        round_urls.append(target_url)
                        
                        print(f"Test cases {test_index + 1} -> {target_url}")
                    
                    tasks = []
                    for i in range(current_round_tests):
                        task = run_single_agent(i+1, round_tasks[i], round_urls[i])
                        tasks.append(task)
                    
                    print(f"Paralleling {len(tasks)} test cases...")
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    
                    round_success = 0
                    round_errors = 0
                    
                    for i, result in enumerate(results):
                        test_index = current_test_index + i
                        if isinstance(result, Exception):
                            round_errors += 1
                            failed_tests += 1
                            result_str = f"Test {test_index + 1}: Error - {str(result)}"
                        else:
                            if result == "Success":
                                round_success += 1
                                successful_tests += 1
                                result_str = ""
                            else:
                                failed_tests += 1
                                result_str = f"Test {test_index + 1}: Failure - {result}"
                        
                        all_results.append(result_str)
                        completed_tests += 1
                    
                    agent_execution_status.update({
                        "completed_tests": completed_tests,
                        "successful_tests": successful_tests,
                        "failed_tests": failed_tests,
                        "current_results": all_results[-current_round_tests:]
                    })
                    
                    print(f"{round_number} rounds completed, {round_success} success, {round_errors} fail")
                    print(f"Completed/Total: {completed_tests}/{total_test_count}")
                    
                    current_test_index += current_round_tests
                
                agent_execution_status.update({
                    "is_running": False,
                    "end_time": time.time()
                })
                
                print(f"\n=== ALL COMPLETED ===")
                print(f"Success {successful_tests}, Fail {failed_tests}")
                global vali_run_counter
                
                if failed_tests == 0:
                    update_csv_results(vali_run_counter, file_name, successful_tests, failed_tests)
                    return "Success"
                else:
                    print(f"{successful_tests}/{total_test_count} tests succeeded")
                    try:
                        downloads_path = Path.home() / "Downloads"
                        result_file_name = file_name.replace('.zip', '.txt')
                        result_file_path = downloads_path / result_file_name
                        with open(result_file_path, 'w', encoding='utf-8') as f:
                            f.write(f"test cases: {total_test_count}\n")
                            f.write(f"success: {successful_tests}\n")
                            f.write(f"fail: {failed_tests}\n")
                            f.write("=" * 50 + "\n")
                            for i, result in enumerate(all_results, 1):
                                f.write(f"{i}. {result}\n")
                        print(f"Results saved to: {result_file_path}")
                    except Exception as e:
                        print(f"Error saving the result {str(e)}")

                    update_csv_results(vali_run_counter, file_name, successful_tests, failed_tests)
                    
                    return all_results

            try:
                global vali_run_counter, round_limit
                result = asyncio.run(run_test_rounds())
                stop_all_webapps_pm2()


                if result == "Success":
                    return jsonify({"message": "success", "result": str(result)})
                else:
                    if image and compare_result:
                        feedback_prompt = get_prompt("TESTING_FEEDBACK_IMG", reports=str(result), compare=str(compare_result))
                    else:
                        feedback_prompt = get_prompt("TESTING_FEEDBACK", reports=str(result))
                    return jsonify({"message": "continue", "result": str(feedback_prompt), "model": model, "provider": provider})
            except Exception as e:
                stop_all_webapps_pm2()
                error_prompt = get_prompt("LAUNCHING_FAILED", errors=str(e))
                return jsonify({"message": "error", "result": str(error_prompt)})

        except Exception as e:
            return jsonify({"message": "error", "result": f"Application execution error: {str(e)}"})

    except Exception as e:
        return jsonify({"message": "error", "result": f"ERROR: {str(e)}"})


def valiv1():
    try:
        print("Validating...")
        file_name = request.args.get('fileName')
        if not file_name:
            return jsonify({"message": "error", "result": "fileName NOT found in the request"})
        
        import urllib.parse
        file_name = urllib.parse.unquote(file_name)

        downloads_path = Path.home() / "Downloads"
        zip_file_path = downloads_path / file_name

        if not zip_file_path.exists():
            return jsonify({"message": "error", "result": f" {file_name} non-exist in {zip_file_path}"})

        extract_folder_name = file_name.replace('.zip', '')
        extract_folder_name = extract_folder_name.replace('&', '_and_').replace(' ', '_')
        extract_path = downloads_path / extract_folder_name

        try:
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
        except Exception as e:
            return jsonify({"message": "error", "result": f"Fail to unzip: {str(e)}"})
        try:
            os.chdir(extract_path)

            result_install = subprocess.run(
                ["npm", "install"],
                capture_output=True,
                text=True,
                timeout=300
            )

            if result_install.returncode != 0:
                print("npm install failed:")
                print(result_install.stderr)
                return jsonify({"message": "error", "result": f"npm install failed: {result_install.stderr}"})

            print(result_install.stdout)
            print(result_install.stderr)

            dev_process = subprocess.Popen(
                ["npm", "run", "dev"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            time.sleep(5)

            url = None
            max_attempts = 33
            attempt = 0

            while attempt < max_attempts and url is None:
                if dev_process.poll() is not None:
                    stdout, stderr = dev_process.communicate()
                    output = stdout + stderr
                    print("Dev server output:", output)
                    break

                try:
                    line = dev_process.stdout.readline()
                    if line:
                        print("Dev server line:", line.strip())
                        match = re.search(r'Local:\s+(https?://[^\s]+)', line)
                        if match:
                            url = match.group(1)
                            print("Found URL:", url)
                            break
                except:
                    pass

                attempt += 1
                time.sleep(1)

            if url is None:
                try:
                    stderr_line = dev_process.stderr.readline()
                    if stderr_line:
                        print("Dev server stderr:", stderr_line.strip())
                        match = re.search(r'Local:\s+(https?://[^\s]+)', stderr_line)
                        if match:
                            url = match.group(1)
                            print("Found URL in stderr:", url)
                except:
                    pass

            if url is None:
                dev_process.terminate()
                return jsonify({"message": "error", "result": "Unable to find server URL"})

            print("VAL: browser-use")

            global global_test_criteria, llm, browser_session, model, provider

            criteria_json = json.dumps(global_test_criteria, ensure_ascii=False)
            task = get_prompt(
                "WEB_TEST",
                url=url,
                criteria=criteria_json
            )

            async def run_agent():
                agent = Agent(
                    task=task,
                    llm=llm,
                    browser_session=browser_session,
                )
                result = await agent.run(max_steps = 20)
                print(result.final_result())
                try:
                    downloads_path = Path.home() / "Downloads"
                    result_file_name = file_name.replace('.zip', '.txt')
                    result_file_path = downloads_path / result_file_name
                    with open(result_file_path, 'w', encoding='utf-8') as f:
                        f.write(result.final_result())
                    print(f"Results saved to: {result_file_path}")
                except Exception as e:
                    print(f"Error saving the result {str(e)}")

                return result.final_result()

            try:
                result = asyncio.run(run_agent())
                if result == "Success":
                    return jsonify({"message": "success", "result": str(result)})
                else:
                    feedback_prompt = get_prompt("FAILED_FEEDBACK", feedback=str(result))
                    return jsonify({"message": "continue", "result": str(feedback_prompt), "model": model, "provider": provider})
            except Exception as e:
                dev_process.terminate()
                error_prompt = get_prompt("ERROR_FEEDBACK", errors=str(e))
                return jsonify({"message": "error", "result": str(error_prompt)})

        except subprocess.TimeoutExpired:
            return jsonify({"message": "error", "result": "npm install over time"})
        except Exception as e:
            return jsonify({"message": "error", "result": f"npm install ERROR: {str(e)}"})

    except Exception as e:
        return jsonify({"message": "error", "result": f"ERROR: {str(e)}"})

if __name__ == '__main__':
    app.run(debug=True)