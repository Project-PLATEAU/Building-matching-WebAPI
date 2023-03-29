import logging
from logging.config import dictConfig

from flask import Flask
from flask_cors import CORS
from werkzeug.serving import WSGIRequestHandler

__version__ = "1.0.0rc1"

# ログ出力形式を指定
dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s(%(lineno)d): %(message)s',
    }},
    'handlers': {'wsgi': {
        'class': 'logging.StreamHandler',
        'stream': 'ext://flask.logging.wsgi_errors_stream',
        'formatter': 'default'
    }},
    'root': {
        'level': 'WARNING',
        'handlers': ['wsgi']
    },
    'loggers': {'app': {
        'level': 'INFO',
        'handlers': ['wsgi'],
        'propagate': False
    }}
})


def create_app():
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    app = Flask(__name__)
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    app.config['SECRET_KEY'] = 'rDSbd8UXdJqtVAE6'
    app.config['JSON_SORT_KEYS'] = False

    from .app import app as app_blueprint
    app.register_blueprint(app_blueprint)

    from .api import api as api_blueprint
    app.register_blueprint(api_blueprint)

    # Prevent jsonify from escaping utf-8 characters
    app.config.update({
        'JSON_AS_ASCII': False,
        'JSONIFY_PRETTYPRINT_REGULAR': False,
    })

    return app


def set_loglevel(level=logging.WARNING):
    logger = logging.getLogger('werkzeug')
    logger.setLevel(level)


set_loglevel()
