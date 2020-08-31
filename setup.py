from setuptools import setup, find_packages

setup(
    name='xeda',

    # Versions should comply with PEP440.  For a discussion on single-sourcing
    # the version across setup.py and the project code, see
    # https://packaging.python.org/en/latest/single_source_version.html
    # version='0.0.1',

    version_config={
        "version_style": {
            "style": "semver",
            "dirty": True,
        },
    },

    description='Simulate And Synthesize HDL!',
    long_description='Simplified automation of simulation and synthesis flows targeting FPGA and ASIC, utilizing both commercial and open-source EDA tools.',

    # The project's main homepage.
    url='https://github.com/kammoh/xeda',


    # Author details
    author='Kamyar Mohajerani',
    author_email='kamyar@ieee.org',

    license='Apache-2.0',
    # https://www.apache.org/licenses/LICENSE-2.0.txt
    # https://opensource.org/licenses/Apache-2.0

    # See https://pypi.python.org/pypi?%3Aaction=list_classifiers
    classifiers=[
        # How mature is this project? Common values are
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 3 - Alpha',

        # Indicate who your project is intended for
        'Intended Audience :: Science/Research',
        'Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)',


        # Pick your license as you wish (should match "license" above)
        'License :: OSI Approved :: Apache Software License',

        # Specify the Python versions you support here. In particular, ensure
        # that you indicate whether you support Python 2, Python 3 or both.
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
    ],

    # What does your project relate to?
    keywords='Synthesis Simuation Hardware EDA Verilog VHDL FPGA ASIC',

    # You can just specify the packages manually here if your project is
    # simple. Or you can use find_packages().
    #packages=find_packages(exclude=['contrib', 'docs', 'tests']),

    packages=find_packages(),

    # Alternatively, if you want to distribute just a my_module.py, uncomment
    # this:
    py_modules=['xeda'],

    python_requires='>=3.6',

    # List run-time dependencies here.  These will be installed by pip when
    # your project is installed. For an analysis of "install_requires" vs pip's
    # requirements files see:
    # https://packaging.python.org/en/latest/requirements.html
    install_requires=[
        "jinja2>=2.11", "color", "progress>=1.5", "coloredlogs"
    ],

    setup_requires=['setuptools-vcs-version'],

    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[dev,test]
    # extras_require={
    #     'dev': [],
    # },

    package_data={"": ['*.tcl', '*.ys', '*.mk']},
    data_files=[('config/xeda',['xeda/defaults.json'])],
    include_package_data=True,

    # To provide executable scripts, use entry points in preference to the
    # "scripts" keyword. Entry points provide cross-platform support and allow
    # pip to create the appropriate form of executable for the target platform.
    entry_points={
        'console_scripts': [
            'xeda=xeda:cli.run_xeda',
        ],
    }
)
