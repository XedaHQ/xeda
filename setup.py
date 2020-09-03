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

    description='XEDA: Cross-platform, cross-tool, cross-target, cross-HDL Electronic Design Automation',
    long_description='''Xeda `/ˈziːdə/` is a cross-platform, cross-EDA, cross-target simulation and synthesis automation platform.
Xeda can assists hardware developers during verification, evaluation, and deployment of RTL designs. Xeda supports flows from multiple commercial and open-source electronic design automation suites.''',

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
        #   3 - Alpha
        #   4 - Beta
        #   5 - Production/Stable
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Science/Research',
        'Topic :: Scientific/Engineering :: Electronic Design Automation (EDA)',
        'License :: OSI Approved :: Apache Software License',
        'Topic :: Utilities',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
    ],

    keywords='EDA Synthesis Simulation Hardware Verilog VHDL FPGA ASIC',
    packages=find_packages(),

    # Alternatively, if you want to distribute just a my_module.py, uncomment
    # this:
    # py_modules=['xeda'],

    python_requires='>=3.6, <4',

    # List run-time dependencies here.  These will be installed by pip when
    # your project is installed. For an analysis of "install_requires" vs pip's
    # requirements files see:
    # https://packaging.python.org/en/latest/requirements.html
    install_requires=[
        "jinja2>=2.11.2", "colored", "progress>=1.5", "coloredlogs"
    ],

    setup_requires=['setuptools-vcs-version'],

    # List additional groups of dependencies here (e.g. development
    # dependencies). You can install these using the following syntax,
    # for example:
    # $ pip install -e .[dev,test]
    # extras_require={
    #     'dev': [],
    # },

    package_data={"":
                  [
                      'xeda/flows/*/templates/*.tcl',
                      'xeda/flows/*/templates/*.sdc',
                      'xeda/flows/vivado/templates/*.xdc',
                      'xeda/flows/quartus/templates/*.dse',
                      'xeda/defaults.json',
                      '*.ys', '*.mk', '*.xdc', '*.sdf', '*.tcl', '*.json'
                  ]
                  },
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
