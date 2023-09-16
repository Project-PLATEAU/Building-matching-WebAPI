from pathlib import Path

from flask import Blueprint, render_template
import markdown

from . import __version__


app = Blueprint('app', __name__, url_prefix='/')

@app.context_processor
def inject_versions():
    return {
        "webapi_version": __version__,
    }


@app.route('/match2d')
def match2d():
    return render_template('match2d.html', geojson=None)


@app.route('/match3d')
def match3d():
    return render_template('match3d.html')


@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def index(path):
    readme_html = 'README'
    with open(Path(__name__).parent / "README.md") as f:
        readme_html = markdown.markdown(f.read())

    return render_template(
        'index.html',
        readme_html=readme_html)
