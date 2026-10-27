from flask import Flask, request
from . import charlie
from .scrape_api import scrape_bp

def create_app():
    app = Flask(__name__)
    app.register_blueprint(scrape_bp)  # adds POST /scrape (the browser calls it as /api/scrape)

    @app.route("/")
    def index():
        return "Flask Server is alive!"

    @app.route('/chat')
    def chat():
        print("/chat called")
        question = request.args.get('question')
        if question:
            answer = charlie.get_answer(question)
            return answer
        else:
            return "question  parameter is missing"

    @app.route('/train')
    def train():
        print("/train called")
        return charlie.train()

    return app
