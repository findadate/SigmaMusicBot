#!/bin/bash
echo "Installing dependencies..."
pip install -r requirements.txt

echo "Starting Sigma Music Bot..."
python3 main.py
