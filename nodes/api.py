import uvicorn
# from lumi.api.main import app

import argparse

def run(host="0.0.0.0"):
    uvicorn.run("lumi.api.main:app", host=host, port=8000, workers=10)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument("--host", default="0.0.0.0", help="Host to run the server on")
    args = parser.parse_args()
    
    run(host=args.host)

