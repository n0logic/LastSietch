#!/usr/bin/env python3
"""Generate hero banner art for Last Sietch game pages via ComfyUI API."""

import json
import time
import urllib.request
import urllib.parse
import os
from PIL import Image
from io import BytesIO

COMFYUI_URL = "http://localhost:8000"
OUTPUT_DIR = "$HOME/Source/Personal/House0fL0gic/website/assets"

def make_workflow(prompt_text, filename_prefix, width=1344, height=512, seed=None):
    """Build a FLUX Schnell workflow for ComfyUI API."""
    if seed is None:
        seed = int(time.time() * 1000) % (2**32)

    return {
        "3": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {
                "ckpt_name": "flux1-schnell-fp8.safetensors"
            }
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": prompt_text,
                "clip": ["3", 1]
            }
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {
                "text": "",
                "clip": ["3", 1]
            }
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {
                "width": width,
                "height": height,
                "batch_size": 1
            }
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["3", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
                "seed": seed,
                "steps": 4,
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0
            }
        },
        "9": {
            "class_type": "VAEDecode",
            "inputs": {
                "samples": ["8", 0],
                "vae": ["3", 2]
            }
        },
        "10": {
            "class_type": "SaveImage",
            "inputs": {
                "images": ["9", 0],
                "filename_prefix": filename_prefix
            }
        }
    }

def queue_prompt(workflow):
    """Submit workflow to ComfyUI and return prompt_id."""
    data = json.dumps({"prompt": workflow}).encode('utf-8')
    req = urllib.request.Request(
        f"{COMFYUI_URL}/api/prompt",
        data=data,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    return result["prompt_id"]

def wait_for_completion(prompt_id, timeout=120):
    """Poll history until prompt completes."""
    start = time.time()
    while time.time() - start < timeout:
        resp = urllib.request.urlopen(f"{COMFYUI_URL}/api/history/{prompt_id}")
        history = json.loads(resp.read())
        if prompt_id in history:
            return history[prompt_id]
        time.sleep(2)
    raise TimeoutError(f"Prompt {prompt_id} didn't complete in {timeout}s")

def get_image(filename, subfolder, folder_type="output"):
    """Download generated image from ComfyUI."""
    params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": folder_type})
    resp = urllib.request.urlopen(f"{COMFYUI_URL}/api/view?{params}")
    return resp.read()

def save_as_webp(image_data, output_path, target_width=1920, target_height=480):
    """Resize and save as WebP."""
    img = Image.open(BytesIO(image_data))
    img = img.resize((target_width, target_height), Image.LANCZOS)
    img.save(output_path, "WebP", quality=85)
    # Also save PNG for archive
    png_path = output_path.replace(".webp", ".png")
    img.save(png_path, "PNG")
    print(f"  Saved: {output_path} ({os.path.getsize(output_path)//1024}KB)")
    print(f"  Saved: {png_path} ({os.path.getsize(png_path)//1024}KB)")
    return img

# --- PROMPTS ---

HEROES = {
    "dune-hero": {
        "prompt": (
            "epic wide panoramic landscape of Arrakis desert, massive sandworm emerging from sand dunes, "
            "spice harvester in distance, orange-gold atmospheric haze, twin sunset, "
            "Fremen silhouettes on ridge, swirling sand particles, cinematic sci-fi concept art, "
            "dramatic lighting, deep shadows, dark moody atmosphere, "
            "ultra detailed digital matte painting, 8k, widescreen banner composition"
        ),
        "width": 1344,
        "height": 512,
        "target_w": 1920,
        "target_h": 480,
    },
    "conan-hero": {
        "prompt": (
            "epic wide panoramic dark fantasy landscape, ancient Hyborian ruins on volcanic mountainside, "
            "barbarian warrior silhouette standing on cliff edge overlooking vast wilderness, "
            "massive stone temple ruins with glowing red runes, stormy dramatic sky with lightning, "
            "molten lava rivers below, dark foreboding atmosphere, "
            "Conan the Barbarian world, cinematic concept art, "
            "ultra detailed digital matte painting, 8k, widescreen banner composition"
        ),
        "width": 1344,
        "height": 512,
        "target_w": 1920,
        "target_h": 480,
    },
    "ql-hero": {
        "prompt": (
            "epic wide panoramic arena shooter environment, gothic sci-fi arena interior, "
            "floating platforms over void, glowing neon railgun trails blue and green, "
            "rocket explosion impact with orange fire, dark metallic architecture, "
            "quad damage powerup glowing purple, jump pads, teleporter portals, "
            "Quake arena tournament combat, dark industrial atmosphere, "
            "cinematic FPS game concept art, ultra detailed, 8k, widescreen banner composition"
        ),
        "width": 1344,
        "height": 512,
        "target_w": 1920,
        "target_h": 480,
    },
}

# Also generate section accent art (wider texture strips)
ACCENTS = {
    "dune-accent": {
        "prompt": (
            "seamless horizontal desert sand texture with subtle spice orange glow, "
            "sand ripples and wind patterns, dark atmospheric, abstract minimal, "
            "dark background fading to black edges, digital art texture"
        ),
        "width": 1344,
        "height": 384,
        "target_w": 1920,
        "target_h": 320,
    },
    "conan-accent": {
        "prompt": (
            "seamless horizontal dark stone texture with carved ancient runes glowing red, "
            "weathered volcanic rock surface, cracks with ember glow, "
            "dark background fading to black edges, fantasy game texture"
        ),
        "width": 1344,
        "height": 384,
        "target_w": 1920,
        "target_h": 320,
    },
    "ql-accent": {
        "prompt": (
            "seamless horizontal dark metallic arena floor texture with neon light strips, "
            "industrial grating with orange and blue glow underneath, "
            "sci-fi game environment texture, dark background fading to black edges"
        ),
        "width": 1344,
        "height": 384,
        "target_w": 1920,
        "target_h": 320,
    },
}

def generate_batch(items, label):
    """Generate a batch of images."""
    print(f"\n{'='*60}")
    print(f"Generating {label}...")
    print(f"{'='*60}")

    prompt_ids = {}
    for name, config in items.items():
        print(f"\nQueuing: {name}")
        print(f"  Prompt: {config['prompt'][:80]}...")
        workflow = make_workflow(
            config["prompt"], name,
            width=config["width"], height=config["height"]
        )
        pid = queue_prompt(workflow)
        prompt_ids[name] = (pid, config)
        print(f"  Queued: {pid}")

    results = {}
    for name, (pid, config) in prompt_ids.items():
        print(f"\nWaiting for {name}...")
        history = wait_for_completion(pid, timeout=180)

        # Extract output image
        outputs = history["outputs"]["10"]["images"]
        img_info = outputs[0]
        image_data = get_image(img_info["filename"], img_info["subfolder"])

        output_path = os.path.join(OUTPUT_DIR, f"{name}.webp")
        img = save_as_webp(
            image_data, output_path,
            target_width=config["target_w"],
            target_height=config["target_h"]
        )
        results[name] = img

    return results

if __name__ == "__main__":
    print("ComfyUI Hero Art Generator for Last Sietch")
    print(f"Output directory: {OUTPUT_DIR}")

    # Generate heroes first
    heroes = generate_batch(HEROES, "Hero Banners")

    # Then accent textures
    accents = generate_batch(ACCENTS, "Accent Textures")

    print(f"\n{'='*60}")
    print("ALL DONE!")
    print(f"{'='*60}")
    for name in list(HEROES.keys()) + list(ACCENTS.keys()):
        path = os.path.join(OUTPUT_DIR, f"{name}.webp")
        if os.path.exists(path):
            size = os.path.getsize(path) // 1024
            print(f"  {name}.webp — {size}KB")
