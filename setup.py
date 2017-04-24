from distutils.core import setup
setup(
    name="yamintapi",
    packages=["yamintapi"],
    version="0.0.1",
    description="Data scrapper API for Mint.com",
    author="Xinlu Huang",
    install_requires=[
        'selenium',
        'requests',
    ]
)
