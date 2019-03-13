#!/usr/bin/python3
# coding: utf-8

from setuptools import setup, find_packages


setup(name='pybtc',
      version='2.0.9',
      description='Python Bitcoin library',
      keywords='bitcoin',
      url='https://github.com/bitaps-com/pybtc',
      author='Alexsei Karpov',
      author_email='admin@bitaps.com',
      license='GPL-3.0',
      package_dir={'':'src'},
      packages=['core', 'pybtc'],
      install_requires=[ 'secp256k1'],
      include_package_data=True,
      package_data={
          'pybtc': ['bip39_word_list/*.txt', 'test/*.txt'],
      },
      test_suite='tests',
      zip_safe=False)
