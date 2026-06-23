import json
import os
import datetime
from flask import Flask, request, jsonify, send_from_directory
from functools import wraps
import hashlib
from pathlib import Path
import re
from collections import defaultdict

# ───────────── CONFIGURACIÓN ─────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage" / "hotel_manager_20"
