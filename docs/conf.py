"""Sphinx docs configuration file."""

# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
import datetime as dt
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath('..'))
year = dt.date.today().year

# -- Project information -----------------------------------------------------

project = 'fractal'
author = 'Plasma AI'
copyright = f'{year}: {author}'

# The full version, including alpha/beta/rc tags
release = importlib.import_module('fractal').__version__

# -- Configure extensions ----------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom ones.
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.napoleon',
    'sphinx_autodoc_typehints',
    'sphinx.ext.todo',
    'sphinx.ext.viewcode',
    'sphinx.ext.mathjax',
]

# List packages which will not (in general) be installed
# when generating documentation with Autodoc.
autodoc_mock_imports = []

# Autodoc settings
autodoc_member_order = 'bysource'
autodoc_default_options = {'ignore-module-all': True}
autoclass_content = 'both'
suppress_warnings = ['autodoc.import_object']

# Napoleon settings
napoleon_google_docstring = True
napoleon_numpy_docstring = False
napoleon_include_init_with_doc = False
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = False
napoleon_use_admonition_for_examples = False
napoleon_use_admonition_for_notes = False
napoleon_use_admonition_for_references = False
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = False
napoleon_type_aliases = None
napoleon_attr_annotations = True

# sphinx-autodoc-typehints settings
always_document_param_types = True

# Autosummary settings
autosummary_generate = True

# Viewcode settings
viewcode_follow_imported_members = True

# -- General configuration ---------------------------------------------------

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# The suffix of source filenames.
source_suffix = '.rst'

# Changed from `master_doc` in Sphinx 4
root_doc = 'index'

# The reST default role (used for this markup: `text`) to use for all documents.
default_role = 'autolink'

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['modules.rst']

# -- Options for HTML output -------------------------------------------------

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
html_static_path = ['_static']

# Refer to the following links for pydata-sphinx-theme options:
# Configuration (https://pydata-sphinx-theme.readthedocs.io/en/latest/user_guide/configuring.html)
# Customization (https://pydata-sphinx-theme.readthedocs.io/en/latest/user_guide/customizing.html)
html_theme = 'pydata_sphinx_theme'

html_favicon = '_static/favicon.png'
html_css_files = ['https://use.typekit.net/zfv6aax.css', 'custom.css']
html_title = 'Fractal Documentation'
html_file_suffix = '.html'

htmlhelp_basename = project

# Additional options
html_context = {
    'default_mode': 'dark',
}
html_theme_options = {
    # Logo variants for light/dark mode, shown beside the project title
    'logo': {
        'text': 'Fractal',
        'image_light': '_static/logo-light.png',
        'image_dark': '_static/logo-dark.png',
    },
    # Show previous/next at page bottom
    'show_prev_next': True,
    # Search bar options
    'search_bar_text': 'Search the docs ...',
    # Navigation options
    'navigation_with_keys': True,
    # Show more levels of the in-page TOC by default
    'show_toc_level': 2,
    # Configure navbar menu item alignment
    'navbar_align': 'content',
    # Enable 'Edit this Page' button (see configuration below)
    'use_edit_page_button': False,
    # Icons with links to show at top of page
    'icon_links': [
        {
            'name': 'GitHub',
            'url': 'https://github.com/plasma-ai/fractal',
            'icon': 'fa-brands fa-github',
            'type': 'fontawesome',
        },
    ],
    # Other links without icons
    'external_links': [],
    # Keep the footer to the copyright line
    'footer_start': ['copyright'],
    'footer_end': [],
}
