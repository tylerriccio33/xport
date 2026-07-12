import polars as pl

import xport
import xport.v56 as v56

def test_write_and_read_polars_dataframe():
    df = pl.DataFrame({'X': [1.0, 2.0, 3.0]})
    ds = xport.Dataset(df, name='POLARS')
    bytestring = v56.dumps(ds)

    library = v56.loads(bytestring, native_namespace=pl)
    got = library['POLARS']

    assert isinstance(got, pl.DataFrame)
    assert got['X'].to_list() == [1.0, 2.0, 3.0]
