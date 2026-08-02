import json
import urllib.request
import urllib.parse
import time
import os
import random

COMFYUI_URL = "http://localhost:8000"
OUTPUT_DIR = "/mnt/c/Users/the operator/Documents/ComfyUI/output"

EMOJIS = [
    # Conan Exiles
    ("emoji_conan_sword", "pixel art emoji of a glowing barbarian greatsword, stylized weapon icon, dark background, game icon style, sharp details, vibrant colors"),
    ("emoji_conan_skull", "pixel art emoji of a skull on a pike, barbarian trophy, dark background, game icon style, sharp details, vibrant orange glow"),
    ("emoji_conan_thrall", "pixel art emoji of a chained thrall slave, Conan Exiles style, dark background, game icon, stylized"),
    ("emoji_conan_base", "pixel art emoji of a sandstone fortress tower, Conan Exiles building, dark background, game icon style"),

    # Dune Awakening
    ("emoji_dune_worm", "pixel art emoji of a giant sandworm emerging from desert sand, Dune style, dark background, epic scale, orange spice glow"),
    ("emoji_dune_rider", "pixel art emoji of a person riding a sandworm, Dune desert scene, dark background, stylized game icon"),
    ("emoji_dune_spice", "pixel art emoji of glowing orange spice melange crystals, Dune style, dark background, mystical glow"),
    ("emoji_dune_fremen", "pixel art emoji of a Fremen warrior with blue eyes and stillsuit, Dune style, dark background"),

    # Quake Live
    ("emoji_ql_frag", "pixel art emoji of an explosion frag kill, Quake arena shooter style, dark background, red orange blast, game icon"),
    ("emoji_ql_rocket", "pixel art emoji of a rocket launcher projectile with smoke trail, Quake style, dark background, fast action"),
    ("emoji_ql_railgun", "pixel art emoji of a blue railgun beam shot, Quake arena style, dark background, neon blue streak"),
    ("emoji_ql_quad", "pixel art emoji of a glowing blue quad damage powerup, Quake style, dark background, electric blue aura"),

    # Last Sietch Branding
    ("emoji_ls_logo", "pixel art emoji of a neon green glowing skull with circuit board patterns, cyberpunk hacker style, dark background, Last Sietch branding"),
    ("emoji_ls_terminal", "pixel art emoji of a green terminal screen with code scrolling, hacker aesthetic, dark background, matrix style"),
    ("emoji_ls_shield", "pixel art emoji of a cyberpunk shield with green circuit lines, dark background, security icon"),

    # Reactions
    ("emoji_gg", "pixel art emoji text saying GG in bold neon green letters, dark background, gaming victory, glowing"),
    ("emoji_rekt", "pixel art emoji text saying REKT in bold red letters with explosion, dark background, gaming"),
    ("emoji_loot", "pixel art emoji of a glowing treasure chest overflowing with gold, dark background, RPG loot drop"),
    ("emoji_rage", "pixel art emoji of an angry red face with steam coming out of ears, dark background, gamer rage"),
    ("emoji_clutch", "pixel art emoji text saying CLUTCH in bold gold letters with sparkles, dark background, gaming victory"),
    ("emoji_noob", "pixel art emoji of a confused character with question marks, cute style, dark background, gaming newbie"),
    ("emoji_ez", "pixel art emoji text saying EZ in bold green neon letters, smug style, dark background"),
    ("emoji_f", "pixel art emoji of a keyboard F key being pressed, pay respects meme, dark background, glowing"),
]

WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "cfg": 1.0,
            "denoise": 1.0,
            "latent_image": ["5", 0],
            "model": ["11", 0],
            "negative": ["7", 0],
            "positive": ["6", 0],
            "sampler_name": "euler",
            "scheduler": "simple",
            "seed": 0,
            "steps": 4
        }
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {
            "batch_size": 1,
            "height": 512,
            "width": 512
        }
    },
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "clip": ["11", 1],
            "text": ""
        }
    },
    "7": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "clip": ["11", 1],
            "text": ""
        }
    },
    "8": {
        "class_type": "VAEDecode",
        "inputs": {
            "samples": ["3", 0],
            "vae": ["11", 2]
        }
    },
    "9": {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": "",
            "images": ["8", 0]
        }
    },
    "11": {
        "class_type": "CheckpointLoaderSimple",
        "inputs": {
            "ckpt_name": "flux1-schnell-fp8.safetensors"
        }
    }
}


def queue_prompt(prompt_data):
    data = json.dumps({"prompt": prompt_data}).encode('utf-8')
    req = urllib.request.Request(f"{COMFYUI_URL}/prompt", data=data, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req)
    return json.loads(resp.read())


def wait_for_completion(prompt_id, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        req = urllib.request.Request(f"{COMFYUI_URL}/history/{prompt_id}")
        resp = urllib.request.urlopen(req)
        history = json.loads(resp.read())
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(2)
    raise TimeoutError(f"Prompt {prompt_id} timed out")


def generate_emoji(name, prompt_text):
    workflow = json.loads(json.dumps(WORKFLOW))
    workflow["6"]["inputs"]["text"] = prompt_text
    workflow["9"]["inputs"]["filename_prefix"] = name
    workflow["3"]["inputs"]["seed"] = random.randint(0, 2**32)

    print(f"  Queuing: {name}")
    result = queue_prompt(workflow)
    prompt_id = result["prompt_id"]

    print(f"  Waiting for {prompt_id}...")
    history = wait_for_completion(prompt_id)
    print(f"  Done: {name}")
    return history


if __name__ == "__main__":
    print(f"Generating {len(EMOJIS)} emojis...")
    for name, prompt in EMOJIS:
        try:
            generate_emoji(name, prompt)
        except Exception as e:
            print(f"  ERROR on {name}: {e}")
    print("All done!")
