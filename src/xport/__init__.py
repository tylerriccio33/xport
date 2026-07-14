"""
Read and write SAS XPORT/XPT-format files.
"""

# Standard Library
import enum
import logging
import re
import string
import struct
import textwrap
import warnings
from collections.abc import Mapping, MutableMapping
from datetime import datetime
from io import StringIO

import narwhals as nw

from .__about__ import __version__  # noqa: F401 module imported but unused

LOG = logging.getLogger(__name__)

__all__ = [
    'Library',
    'Member',
    'NaN',
]


class SpecialMissingValue(float):
    """
    Special missing values.

    SAS supports 27 special missing values, allowing the categorization of
    missing data by tagging or labeling missing values using the letters A to Z
    or an underscore.
    """

    pattern = re.compile(b'^(?P<tag>[A-Z_])' + b'\x00' * 7 + b'$')

    def __bytes__(self):
        """
        XPORT-format byte string.
        """
        return self.name.encode('ascii') + b'\x00' * 7

    @classmethod
    def unpack(cls, bytestring):
        """
        Create a ``SpecialMissingValue`` from an XPORT-format bytestring.

        For compatibility with standard Python tools, ``xport.v56.ibm_to_ieee``
        creates regular float NaNs rather than special missing values.
        """
        match = cls.pattern.match(bytestring)
        return NaN(match['tag'].decode('ascii'))


NaN = enum.Enum(
    value='NaN',
    names={c: float('nan')
           for c in '_' + string.ascii_uppercase},
    module=__name__,
    type=SpecialMissingValue,
)
NaN.__doc__ = SpecialMissingValue.__doc__


class VariableType(enum.IntEnum):
    """
    SAS variables can be either Numeric or Character type.
    """
    NUMERIC = 1
    CHARACTER = 2


class FormatAlignment(enum.IntEnum):
    """
    SAS formats are either left- or right-aligned.
    """
    LEFT = 0
    RIGHT = 1


class Informat:
    """
    SAS variable informat.
    """

    pattern = re.compile(
        r'^(?P<name>\$?[A-Z0-9]*?)(?P<w>\d+)\.(?P<d>\d+)?$',
        re.IGNORECASE,
    )

    #    char8 niform;      /* NAME OF INPUT FORMAT                   */
    #    short nifl;        /* INFORMAT LENGTH ATTRIBUTE              */
    #    short nifd;        /* INFORMAT NUMBER OF DECIMALS            */
    byte_structure = '>8shh'

    def __init__(self, name='', length=0, decimals=0):
        """
        Initialize an input format.
        """
        self._name = name
        self._length = length
        self._decimals = decimals

    def __str__(self):
        """
        Pleasant display value.
        """
        if not (self.name or self.length or self.decimals):
            return ''
        decimals = self.decimals if self.decimals else ''
        return f'{self.name}{self.length}.{decimals}'

    def __repr__(self):
        """
        REPL-format string.
        """
        return '{cls}(name={name!r}, length={length!r}, decimals={decimals!r})'.format(
            cls=type(self).__name__,
            name=self.name,
            length=self.length,
            decimals=self.decimals,
        )

    def __bytes__(self):
        """
        XPORT-format byte string.
        """
        fmt = self.byte_structure
        name = self.name.encode('ascii').ljust(8)
        if len(name) > 8:
            raise ValueError('ASCII-encoded {name!r} longer than 8 bytes')
        return struct.pack(fmt, name, self.length, self.decimals)

    @classmethod
    def unpack(cls, bytestring):
        """
        Create an informat from an XPORT-format bytestring.
        """
        fmt = cls.byte_structure
        return cls.from_struct_tokens(*struct.unpack(fmt, bytestring))

    @classmethod
    def from_struct_tokens(cls, name, length, decimals):
        """
        Create an informat from unpacked struct tokens.
        """
        name = name.strip(b'\x00').decode('ascii').strip()
        return cls(name=name, length=length, decimals=decimals)

    @classmethod
    def from_spec(cls, spec, *args, **kwds):
        """
        Create an informat from a text specification.
        """
        mo = cls.pattern.fullmatch(spec)
        if mo is None:
            raise ValueError(f'Invalid informat {spec}')
        name = mo['name'].upper()
        bytestring = name.encode('ascii')
        if len(bytestring) > 8:
            raise ValueError(f'ASCII-encoded name {bytestring} longer than 8 characters')
        length = int(mo.group('w'))
        try:
            decimals = int(mo.group('d'))
        except (TypeError, IndexError):
            decimals = 0
        form = cls(*args, name=name, length=length, decimals=decimals, **kwds)
        LOG.debug(f'Parsed {form!r} from {spec!r}')
        return form

    @property
    def name(self):
        """The name of the format."""  # noqa: D401
        return self._name

    @property
    def length(self):
        """The width value of the format: ``INFORMATw.``."""  # noqa: D401
        return self._length

    @property
    def decimals(self):
        """The ``d`` value of numeric formats: ``INFORMATw.d``."""  # noqa: D401
        # The documentation states that ``d`` optionally specifies the
        # power of 10 by which to divide numeric input.  If the data
        # contain decimal points, the ``d``` value is ignored.
        return self._decimals

    def __eq__(self, other):
        """Equality."""
        if not isinstance(other, Informat):
            raise TypeError(f"Can't compare {type(self).__name__} with {type(other).__name__}")
        attributes = [
            'name',
            'length',
            'decimals',
        ]
        return all(getattr(self, a) == getattr(other, a) for a in attributes)


class Format(Informat):
    """
    SAS variable format.
    """

    #    char8 nform;       /* NAME OF FORMAT                         */
    #    short nfl;          /* FORMAT FIELD LENGTH OR 0               */
    #    short nfd;         /* FORMAT NUMBER OF DECIMALS              */
    #    short nfj;         /* 0=LEFT JUSTIFICATION, 1=RIGHT JUST     */
    byte_structure = '>8shhh'

    def __init__(self, name='', length=0, decimals=0, justify=FormatAlignment.LEFT):
        """
        Initialize a SAS variable format.
        """
        self._justify = justify
        super().__init__(name, length, decimals)

    def __repr__(self):
        """
        REPL-format string.
        """
        fmt = '{cls}(name={name!r}, length={length!r}, decimals={decimals!r}, justify={justify})'
        return fmt.format(
            cls=type(self).__name__,
            name=self.name,
            length=self.length,
            decimals=self.decimals,
            justify=self.justify,
        )

    def __bytes__(self):
        """
        XPORT-format byte string.
        """
        # TODO: It'd be nice to avoid copy-pasting code from parent.
        fmt = self.byte_structure
        name = self.name.encode('ascii').ljust(8)
        if len(name) > 8:
            raise ValueError('ASCII-encoded {name!r} longer than 8 bytes')
        length = self.length if self.length is not None else 0
        decimals = self.decimals if self.decimals is not None else 0
        return struct.pack(fmt, name, length, decimals, self.justify)

    @classmethod
    def from_struct_tokens(cls, name, length, decimals, justify):
        """
        Create a format from unpacked struct tokens.
        """
        form = super().from_struct_tokens(name, length, decimals)
        form._justify = justify
        return form

    @classmethod
    def from_spec(cls, spec, justify=FormatAlignment.LEFT):
        """
        Create a format from a text specification.
        """
        return super().from_spec(spec=spec, justify=justify)

    @property
    def justify(self):
        """
        Left- or right-alignment.
        """
        return self._justify

    def __eq__(self, other):
        """Equality."""
        return super().__eq__(other) and self.justify == other.justify




def _coerce_unknown_dtypes(frame_or_series):
    """
    Narwhals reports pandas' native ``str`` dtype (pandas >= 2.something's
    ``pd.StringDtype``-like default) as ``Unknown``. Downcast such columns
    to ``object`` so Narwhals can infer them as ``String`` instead.
    """
    if frame_or_series.implementation.name != 'PANDAS':
        return frame_or_series
    if isinstance(frame_or_series, nw.Series):
        if frame_or_series.dtype == nw.Unknown:
            native = frame_or_series.to_native().astype(object)
            return nw.from_native(native, series_only=True)
        return frame_or_series
    unknown_columns = [name for name, dtype in frame_or_series.schema.items() if dtype == nw.Unknown]
    if not unknown_columns:
        return frame_or_series
    native = frame_or_series.to_native().astype({name: object for name in unknown_columns})
    return nw.from_native(native, eager_only=True)


#: Backend used to build a frame from scratch when the caller hasn't
#: supplied a ``native_namespace`` (e.g. ``Dataset()``, ``Dataset({...})``).
#: Set this to a backend name (e.g. ``'pandas'``) if Polars isn't installed.
DEFAULT_BACKEND = 'polars'


def _resolve_native_namespace(backend=None):
    """
    Resolve ``backend`` (a native module, ``nw.Implementation``, backend name
    like ``'polars'``, or ``None``) to its native dataframe module, via
    Narwhals' own backend registry rather than importing the library
    ourselves.
    """
    if backend is None:
        backend = DEFAULT_BACKEND
    if hasattr(backend, 'DataFrame'):
        return backend
    return nw.Implementation.from_backend(backend).to_native_namespace()


def _native_series(ns, name, values, **kwds):
    """
    Construct a native series using ``ns``'s own calling convention.

    ``ns`` is a caller-supplied native dataframe library (e.g. an explicit
    ``native_namespace=`` argument), not one imported by this module.
    """
    if getattr(ns, '__name__', '') == 'polars':
        return ns.Series(name=name, values=values, **kwds)
    return ns.Series(values, name=name, **kwds)


class Variable:
    """
    SAS variable.

    ``Variable`` wraps a Narwhals ``Series``, adding SAS metadata. It
    composes a ``nw.Series`` rather than subclassing it: Narwhals offers no
    public subclassing hook, only private compliant-object internals
    (``_compliant_series``, ``_level``) that aren't stable across versions.
    Anything not defined here (``.dtype``, ``.str``, ``.to_list()``, etc.)
    is delegated to the wrapped series via ``__getattr__``.
    """

    _metadata = [
        'label',
        'width',
        'vtype',
        'format',
        'informat',
    ]

    def _get_meta(self, key):
        """
        Read metadata, either from an attached ``Dataset`` column or locally.
        """
        dataset = self.__dict__.get('_dataset')
        if dataset is not None:
            return dataset._column_metadata.get(self.__dict__['_column'], {}).get(key)
        return self.__dict__.get('_local_meta', {}).get(key)

    def _set_meta(self, key, value):
        dataset = self.__dict__.get('_dataset')
        if dataset is not None:
            dataset._column_metadata.setdefault(self.__dict__['_column'], {})[key] = value
        else:
            self.__dict__.setdefault('_local_meta', {})[key] = value

    def copy_metadata(self, other):
        """
        Copy metadata from another Variable.
        """
        # LOG.debug(f'Copying metadata from {other}')  # BUG: Causes infinite recursion!
        if isinstance(other, Variable):
            for name in self._metadata:
                value = self._get_meta(name)
                if value is None:
                    value = getattr(other, name, None)
                if value is not None:
                    self._set_meta(name, value)

    def __repr__(self):
        """REPL-format."""
        metadata = (name.strip('_') for name in self._metadata)
        metadata = {name: getattr(self, name, None) for name in metadata}
        metadata = (f'{name}: {value}' for name, value in metadata.items() if value is not None)
        return f'{type(self).__name__}\n{repr(self.to_native())}\n{", ".join(metadata)}'

    def __init__(
        self,
        data=None,
        *,
        name=None,
        native_namespace=None,
        label=None,
        vtype=None,
        width=None,
        format=None,
        informat=None,
        **kwds,
    ):
        """
        Initialize SAS variable metadata.
        """
        # TODO: Consider validating that the name isn't blank.
        metadata = {
            'label': label,
            'vtype': vtype,
            'width': width,
            'format': format,
            'informat': informat,
        }
        if isinstance(data, Variable):
            native = data.to_native()
        elif isinstance(data, nw.Series):
            native = data.to_native()
        elif data is None:
            native = _native_series(_resolve_native_namespace(native_namespace), name, [])
        else:
            try:
                native = nw.from_native(data, series_only=True).to_native()
            except TypeError:
                ns = _resolve_native_namespace(native_namespace)
                native = _native_series(ns, name, list(data), **kwds)
        if name is not None and native.name != name:
            native = native.rename(name)
        self.__dict__['_series'] = _coerce_unknown_dtypes(nw.from_native(native, series_only=True))
        self.__dict__['_local_meta'] = {}
        for name, value in metadata.items():
            if value is not None:
                setattr(self, name, value)
        self.copy_metadata(data)
        for name, value in metadata.items():
            setattr(self, name, getattr(self, name, value))
        LOG.debug(f'Initialized {self}')

    def __getattr__(self, name):
        """
        Delegate to the wrapped Narwhals series, e.g. ``.dtype``, ``.str``.
        """
        return getattr(self.__dict__['_series'], name)

    def __getitem__(self, item):
        return self.__dict__['_series'][item]

    def __len__(self):
        return len(self.__dict__['_series'])

    def copy(self):
        """
        Copy the variable, keeping its SAS metadata.
        """
        return Variable(self)

    def append(self, other):
        """
        Concatenate with another variable-like object, keeping SAS metadata.
        """
        if isinstance(other, Variable):
            other_native = other.to_native()
        elif isinstance(other, nw.Series):
            other_native = other.to_native()
        else:
            other_native = other
        other_series = nw.from_native(other_native, series_only=True)
        this_series = self._series
        if this_series.dtype != other_series.dtype:
            common = other_series.dtype if len(this_series) == 0 else this_series.dtype
            this_series = this_series.cast(common)
            other_series = other_series.cast(common)
        other_series = other_series.rename(this_series.name)
        combined = nw.concat([this_series.to_frame(), other_series.to_frame()], how='vertical')
        result = Variable(combined[this_series.name])
        result.copy_metadata(self)
        return result

    label = property(
        lambda self: self._get_meta('label'),
        lambda self, value: self._set_meta('label', value),
        doc='SAS variable label.',
    )
    width = property(
        lambda self: self._get_meta('width'),
        lambda self, value: self._set_meta('width', value),
        doc='SAS variable width, in bytes.',
    )
    vtype = property(
        lambda self: self._get_meta('vtype'),
        lambda self, value: self._set_meta('vtype', value),
        doc='SAS variable type, numeric or character.',
    )

    @property
    def format(self):
        """
        SAS variable format.
        """
        return self._get_meta('format')

    @format.setter
    def format(self, value):
        if value is None:
            self._set_meta('format', None)
        elif isinstance(value, Format):
            self._set_meta('format', value)
        else:
            self._set_meta('format', Format.from_spec(value))

        if self.format and self.format.name.startswith('$'):
            self.vtype = VariableType.CHARACTER
        elif self.format and (self.format.name or self.format.decimals):
            self.vtype = VariableType.NUMERIC

    @property
    def informat(self):
        """
        SAS variable informat.
        """
        return self._get_meta('informat')

    @informat.setter
    def informat(self, value):
        if value is None:
            self._set_meta('informat', None)
        elif isinstance(value, Informat):
            self._set_meta('informat', value)
        else:
            self._set_meta('informat', Informat.from_spec(value))

        if self.informat and self.informat.name.startswith('$'):
            self.vtype = VariableType.CHARACTER
        elif self.informat and (self.informat.name or self.informat.decimals):
            self.vtype = VariableType.NUMERIC


class Dataset:
    """
    SAS data set.

    ``Dataset`` wraps a Narwhals ``DataFrame``, adding SAS metadata. Like
    ``Variable``, it composes rather than subclasses Narwhals, for the same
    reason: no public subclassing hook exists. Anything not defined here
    (``.schema``, ``.filter()``, ``.iter_rows()``, etc.) is delegated to the
    wrapped dataframe via ``__getattr__``.
    """

    _metadata = [
        'name',
        'dataset_label',
        'dataset_type',
        'created',
        'modified',
        'sas_os',
        'sas_version',
    ]

    def copy_metadata(self, other):
        """
        Copy metadata from another Dataset.
        """
        if isinstance(other, Dataset):
            for name in self._metadata:
                object.__setattr__(self, name, getattr(other, name, None))

    def __repr__(self):
        """REPL-format."""
        metadata = (name.strip('_') for name in self._metadata)
        metadata = {name: getattr(self, name, None) for name in metadata}
        metadata = (f'{name}: {value}' for name, value in metadata.items() if value)
        template = '''\
            {cls} {name}

            {super}
            {metadata}
        '''
        return textwrap.dedent(template).format(
            cls=type(self).__name__,
            name=self.name,
            super=repr(self.to_native()),
            metadata=', '.join(metadata),
        )

    @property
    def label(self):
        return self.dataset_label

    @label.setter
    def label(self, value):
        self.dataset_label = value

    def __init__(
        self,
        data=None,
        *,
        name=None,
        label=None,
        dataset_label=None,
        dataset_type=None,
        created=None,
        modified=None,
        sas_os=None,
        sas_version=None,
        native_namespace=None,
        **kwds,
    ):
        """
        Initialize SAS dataset metadata.

        ``data`` may be a native dataframe (e.g. a ``polars.DataFrame`` or
        ``pandas.DataFrame``), another ``Dataset``, or a mapping/iterable of
        columns, in which case ``native_namespace`` (default ``polars``)
        determines which dataframe library backs the new ``Dataset``.
        """
        if dataset_label is not None:
            label = dataset_label
            warnings.warn(
                'Use ``label`` instead of ``dataset_label``',
                DeprecationWarning,
                stacklevel=2,
            )
        # TODO: Consider validating dataset type: {'DATA', 'VIEW', 'CATALOG'}.
        #       I think only 'DATA' is supported by the XPORT format.
        # TODO: Consider validating that the name isn't blank.
        metadata = {
            'name': name,
            'dataset_label': label,
            'created': created,
            'modified': modified,
            'sas_os': sas_os,
            'sas_version': sas_version,
            'dataset_type': dataset_type,
        }
        if isinstance(data, Dataset):
            native = data.to_native()
        elif isinstance(data, nw.DataFrame):
            native = data.to_native()
        elif data is None:
            native = _resolve_native_namespace(native_namespace).DataFrame(**kwds)
        elif isinstance(data, Mapping):
            columns = {
                k: v.to_native() if isinstance(v, (Variable, nw.Series)) else v
                for k, v in data.items()
            }
            if columns:
                ns = _resolve_native_namespace(native_namespace)
                if getattr(ns, '__name__', '') == 'polars':
                    # Polars infers a column's dtype from its first values, then
                    # raises rather than upcasting if a later value doesn't fit,
                    # e.g. int64 inferred from [0, 1, ...] followed by a float/NaN.
                    columns = {
                        k: ns.Series(k, v, strict=False) if isinstance(v, list) else v
                        for k, v in columns.items()
                    }
                native = nw.from_dict(columns, backend=ns, **kwds).to_native()
            else:
                native = _resolve_native_namespace(native_namespace).DataFrame(**kwds)
        else:
            try:
                native = nw.from_native(data, eager_only=True).to_native()
            except TypeError:
                # Not already a recognized native dataframe; treat it as
                # column/row data to build one from scratch.
                native = _resolve_native_namespace(native_namespace).DataFrame(data, **kwds)
        self.__dict__['_frame'] = _coerce_unknown_dtypes(nw.from_native(native, eager_only=True))
        self._column_metadata = {}
        if isinstance(data, Dataset):
            self._column_metadata = {k: dict(v) for k, v in data._column_metadata.items()}
        elif isinstance(data, Mapping):
            for k, v in data.items():
                if isinstance(v, Variable):
                    self._column_metadata[k] = {
                        name: getattr(v, name)
                        for name in Variable._metadata
                        if getattr(v, name) is not None
                    }
        for attr, value in metadata.items():
            if value is not None:
                setattr(self, attr, value)
        self.copy_metadata(data)
        for attr, value in metadata.items():
            setattr(self, attr, getattr(self, attr, value))
        LOG.debug(f'Initialized {self}')

    def __getattr__(self, name):
        """
        Delegate to the wrapped Narwhals dataframe, e.g. ``.schema``.
        """
        return getattr(self.__dict__['_frame'], name)

    def _wrap(self, frame):
        """
        Construct a new ``Dataset`` (or subclass) around ``frame``,
        preserving SAS metadata.
        """
        obj = object.__new__(type(self))
        obj.__dict__['_frame'] = frame
        for attr in self._metadata:
            setattr(obj, attr, getattr(self, attr, None))
        obj._column_metadata = {k: dict(v) for k, v in self._column_metadata.items()}
        return obj

    def __getitem__(self, item):
        """
        Get a column (attached, so its metadata round-trips) or a slice.
        """
        if isinstance(item, str):
            result = object.__new__(Variable)
            result.__dict__['_series'] = self._frame[item]
            result.__dict__['_dataset'] = self
            result.__dict__['_column'] = item
            return result
        result = self._frame[item]
        if isinstance(result, nw.DataFrame):
            return self._wrap(result)
        return result

    def __setitem__(self, key, value):
        """
        Insert or replace a column, keeping its SAS metadata.
        """
        if isinstance(value, Variable):
            metadata = {
                name: getattr(value, name)
                for name in Variable._metadata
                if getattr(value, name) is not None
            }
            series = value.__dict__['_series']
        elif isinstance(value, nw.Series):
            metadata = {}
            series = value
        else:
            metadata = {}
            series = nw.new_series(key, list(value), native_namespace=self._frame.__native_namespace__())
        if series.name != key:
            series = series.rename(key)
        self.__dict__['_frame'] = self._frame.with_columns(series)
        self._column_metadata[key] = metadata or self._column_metadata.get(key, {})

    def items(self):
        """
        Iterate over ``(column name, Variable)`` pairs.
        """
        for name in self.columns:
            yield name, self[name]

    def with_columns(self, *exprs, **named_exprs):
        """
        Add or replace columns, preserving SAS metadata and ``Dataset`` type.
        """
        return self._wrap(self._frame.with_columns(*exprs, **named_exprs))

    def copy(self):
        """
        Copy the dataset, keeping its SAS metadata.
        """
        return self._wrap(self._frame)

    def append(self, other):
        """
        Concatenate with another dataset-like object, keeping SAS metadata.
        """
        if isinstance(other, Dataset):
            other_native = other.to_native()
        elif isinstance(other, nw.DataFrame):
            other_native = other.to_native()
        else:
            other_native = other
        other_frame = nw.from_native(other_native, eager_only=True)
        if other_frame.implementation != self._frame.implementation:
            other_frame = nw.from_dict(
                other_frame.to_dict(as_series=False), backend=self._frame.implementation
            )
        combined = nw.concat([self._frame, other_frame], how='vertical')
        return self._wrap(combined)

    @property
    def contents(self):
        """
        Variable metadata, such as label, format, number, and position.
        """
        rows = [{
            '#': i,
            'Variable': v.name,
            'Type': v.vtype.name.title() if v.vtype is not None else '',
            'Length': v.width,
            'Format': str(v.format) if v.format is not None else '',
            'Informat': str(v.informat) if v.informat is not None else '',
            'Label': v.label if v.label is not None else '',
        } for i, (k, v) in enumerate(self.items(), start=1)]
        columns = ['#', 'Variable', 'Type', 'Length', 'Format', 'Informat', 'Label']
        data = {col: [row[col] for row in rows] for col in columns}
        frame = nw.from_dict(data, backend=self._frame.implementation)
        if frame.is_empty():
            return frame
        length = frame['Length']
        try:
            length = length.cast(nw.Int64)
        except (TypeError, ValueError):
            pass  # A backend (e.g. Pandas) can't hold nulls in a plain integer column.
        position = length.cum_sum().shift(1).scatter(0, 0)
        try:
            position = position.cast(nw.Int64)
        except (TypeError, ValueError):
            pass
        frame = frame.with_columns(length.alias('Length'), position.alias('Position'))
        return frame.select(['#', 'Variable', 'Type', 'Length', 'Position', 'Format', 'Informat', 'Label'])

    def infos(self):
        """
        Summary of the dataset's columns and dtypes.
        """
        buf = StringIO()
        buf.write(f'{type(self).__name__}: {self.name}\n')
        buf.write(f'Columns: {len(self.columns)}\n')
        for col, dtype in self.schema.items():
            buf.write(f'  {col}: {dtype}\n')
        buf.seek(0)
        return buf.read()


class Library(MutableMapping):
    """
    Collection of datasets from a SAS file.
    """

    def __init__(self, members=(), created=None, modified=None, sas_os='', sas_version=''):
        """
        Initialize a SAS data library.
        """
        if created is None:
            created = datetime.now()
        if modified is None:
            modified = created
        self.created = created
        self.modified = modified
        self.sas_os = sas_os
        self.sas_version = sas_version

        # Convert a single dataset or dataframe to a collection of them.
        if isinstance(members, Dataset):
            members = {members.name: members}
        elif not isinstance(members, (Library, Mapping)):
            try:
                nw.from_native(members, eager_only=True)
            except TypeError:
                pass
            else:
                members = {getattr(members, 'name', None): members}

        self._members = {}
        if isinstance(members, Library):
            self._members = members._members
            self.created = members.created
            self.modified = members.modified
            self.sas_os = members.sas_os
            self.sas_version = members.sas_version
        elif isinstance(members, Mapping):
            for name, dataset in members.items():
                self[name] = dataset  # Use __setitem__ to validate metadata.
        else:
            for dataset in members:
                if dataset.name in self:
                    warnings.warn(f'More than one dataset named {dataset.name!r}')
                self[dataset.name] = dataset

    def __repr__(self):
        """
        REPL-format string.
        """
        fmt = '<{cls} members={members}>'
        return fmt.format(cls=type(self).__name__, members=list(self))

    def __getitem__(self, name):
        """
        Get a member dataset.
        """
        return self._members[name]

    def __setitem__(self, name, dataset):
        """
        Insert or update a member in the library.
        """
        if not isinstance(dataset, Dataset):
            dataset = Dataset(dataset, name=name)
        elif dataset.name is None and name is not None:
            dataset.name = name
            warnings.warn(f'Set dataset name to {name!r}')
        elif name != dataset.name:
            raise ValueError(f'Library member name {name} must match dataset name {dataset.name}')
        self._members[name] = dataset

    def __delitem__(self, name):
        """
        Remove a member datset.
        """
        del self._members[name]

    def __iter__(self):
        """
        Get an iterator of dataset names.
        """
        return iter(self._members)

    def __len__(self):
        """
        Get the number of datasets in the library.
        """
        return len(self._members)



########################################################################
# Legacy, keeping backwards compatibility.


def from_columns(mapping, fp):
    """
    Write columns to the open file opbject ``fp`` in XPT-format.

    The mapping should be of column names to equal-length sequences.

    Column labels are restricted to 40 characters. The XPT format also
    requires a separate column "name" that is restricted to 8
    characters. This name will be automatically created based on the
    column label -- the first 8 characters, non-alphabet characters
    replaced with underscores, padded to 8 characters if necessary.  All
    text strings, including column labels, will be converted to bytes
    using the ISO-8859-1 encoding.
    """
    return from_dataframe(mapping, fp)


def from_rows(iterable, fp):
    """
    Write rows to the open file object ``fp`` in XPT-format.

    In this case, ``rows`` should be an iterable of iterables, such as a
    list of tuples. If the rows are mappings or namedtuples (or any
    instance of a tuple that has a ``._fields`` attribute), the column
    labels will be inferred from the keys or attributes of the first
    row.

    Column labels are restricted to 40 characters. The XPT format also
    requires a separate column "name" that is restricted to 8
    characters. This name will be automatically created based on the
    column label -- the first 8 characters, non-alphabet characters
    replaced with underscores, padded to 8 characters if necessary.  All
    text strings, including column labels, will be converted to bytes
    using the ISO-8859-1 encoding.
    """
    rows = list(iterable)
    first = rows[0] if rows else None
    if first is None:
        data = {}
    elif isinstance(first, Mapping):
        data = {k: [row[k] for row in rows] for k in first}
    elif hasattr(first, '_fields'):
        data = {k: [getattr(row, k) for row in rows] for k in first._fields}
    else:
        data = {f'x{i:02d}': [row[i] for row in rows] for i in range(len(first))}
    ns = _resolve_native_namespace(None)
    if not data:
        native = ns.DataFrame()
    else:
        native = nw.from_dict(data, backend=ns).to_native()
    return from_dataframe(native, fp)


def from_dataframe(dataframe, fp):
    """
    Write a Pandas ``DataFrame`` to an XPORT-format file.

    Serialize to ``fp``, an open file-like object.
    """
    # Avoid circular import problems.
    # Xport Modules
    from xport.v56 import dump
    warnings.warn('Please use ``xport.v56.dump`` in the future', DeprecationWarning)
    library = Library([Dataset(dataframe)])
    dump(library, fp)


def to_rows(fp):
    """
    Read a file in XPT-format and return rows.

    Deserialize ``fp`` (a ``.read()``-supporting file-like object
    containing an XPT document) to a list of rows. As XPT files are
    encoded in their own special format, the ``fp`` object must be in
    bytes-mode. ``Row`` objects will be namedtuples with attributes
    parsed from the XPT metadata.
    """
    df = to_dataframe(fp)
    return list(df.iter_rows(named=False))


def to_columns(fp):
    """
    Read a file in XPT-format and return columns as a dict of lists.

    Deserialize ``fp`` (a ``.read()``-supporting file-like object
    containing an XPT document) to a list of rows. As XPT files are
    encoded in their own special format, the ``fp`` object must be in
    bytes-mode.
    """
    dataset = to_dataframe(fp)
    return {k: dataset[k].to_list() for k in dataset.columns}


def to_numpy(fp):
    """
    Read a file in SAS XPT format and return a NumPy array.

    Deserialize ``fp`` (a ``.read()``-supporting file-like object
    containing an XPT document) to a list of rows. As XPT files are
    encoded in their own special format, the ``fp`` object must be in
    bytes-mode.
    """
    return to_dataframe(fp).to_numpy()


def to_dataframe(fp):
    """
    Read a file in SAS XPT format and return a ``Dataset``.

    Deserialize ``fp`` (a ``.read()``-supporting file-like object
    containing an XPT document) to a list of rows. As XPT files are
    encoded in their own special format, the ``fp`` object must be in
    bytes-mode.

    .. deprecated::
        Despite the name, this no longer returns a Pandas ``DataFrame``
        specifically -- it returns an ``xport.Dataset``, which wraps
        whichever dataframe library you're using (Polars by default).
        Use ``xport.v56.load`` instead.
    """
    # Avoid circular import problems.
    # Xport Modules
    from xport.v56 import load
    warnings.warn('Please use ``xport.v56.load`` in the future', DeprecationWarning)
    library = load(fp)
    dataset = next(iter(library.values()))
    return dataset


class Reader:
    """
    Read records from a SAS Transport (XPORT) file.

    Deserialize ``self._fp`` (a ``.read()``-supporting file-like object
    containing an XPT document) to a Python object.

    The returned object is an iterator.  Each iteration returns an
    observation from the XPT file.

        with open('example.xpt', 'rb') as f:
            for row in xport.Reader(f):
                process(row)
    """

    def __init__(self, fp):
        self.dataset = to_dataframe(fp)

    def __iter__(self):
        return iter(self.dataset.itertuples(index=False, name='Observation'))

    @property
    def fields(self):
        return tuple(self.dataset.columns)

    def __getattr__(self, name):
        return getattr(self.dataset, name)


class XportReader(Reader):
    pass  # Alias.  https://github.com/selik/xport/issues/55


class NamedTupleReader(Reader):
    pass  # Reader already yields namedtuples.


class DictReader(Reader):

    def __iter__(self):
        for row in super().__iter__():
            yield row._asdict()
