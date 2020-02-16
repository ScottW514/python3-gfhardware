from distutils.core import setup, Extension
setup(
    name='gfhardware',
    description='Glowforge Hardware Support',
    author='Scott Wiederhold',
    author_email='s.e.wiederhold@gmail.com',
    url='https://github.com/ScottW514/python3-gfhardware',
    version='0.0.1',
    license='MIT',
    long_description=open('README.md').read(),
    keywords='Glowforge OpenGlow OV5648 imx6',
    packages = ['gfhardware'],
    ext_modules=[
            Extension(
                name='gfhardware.cam',
                sources=['gfhardware/cam.c', 'gfhardware/bayer.c'],
                libraries=["v4l2"])
    ],
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python',
        'Programming Language :: Python :: 3',
        'Operating System :: POSIX :: Linux',
        'Topic :: Software Development :: Embedded Systems',
    ],
)
