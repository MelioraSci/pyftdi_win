# Copyright (c) 2010-2023 Emmanuel Blot <emmanuel.blot@free.fr>
# Copyright (c) 2010-2016, Neotion
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

#pylint: disable-msg=missing-docstring

__version__ = '0.55.0'
__title__ = 'PyFtdi_win'
__description__ = 'FTDI device driver (pure Python)'
__uri__ = 'https://github.com/MelioraSci/pyftdi_win'
__doc__ = __description__ + ' <' + __uri__ + '>'
__author__ = 'Meliora Scientific'
# For all support requests, please open a new issue on GitHub
__email__ = 'info@meliorasci.com'
__license__ = 'Modified BSD'
__copyright__ = 'Copyright (c) 2023 Meliora Scientific Inc'


from logging import WARNING, NullHandler, getLogger


class FtdiLogger:

    log = getLogger('pyftdi_win')
    log.addHandler(NullHandler())
    log.setLevel(level=WARNING)

    @classmethod
    def set_formatter(cls, formatter):
        handlers = list(cls.log.handlers)
        for handler in handlers:
            handler.setFormatter(formatter)

    @classmethod
    def get_level(cls):
        return cls.log.getEffectiveLevel()

    @classmethod
    def set_level(cls, level):
        cls.log.setLevel(level=level)
