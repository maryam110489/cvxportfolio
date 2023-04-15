# Copyright 2023- The Cvxportfolio Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path
import yfinance as yf
import pandas as pd
import numpy as np
import pandas_datareader
import sqlite3


class BaseData:
    """Base class for Cvxportfolio database interface."""

    def load_raw(self, symbol):
        """Load raw data from database."""
        raise NotImplementedError

    def load(self, symbol):
        """Load data from database using `self.preload` function to process it."""
        return self.preload(self.load_raw(symbol))

    def store(self, symbol, data):
        """Store data in database."""
        raise NotImplementedError

    def download(self, symbol, current):
        """Download data from external source."""
        raise NotImplementedError

    def update_and_load(self, symbol):
        """Update current stored data for symbol and load it.
        
        DEPRECATED: separate update and load functionalities.
        """
        current = self.load_raw(symbol)
        updated = self.download(symbol, current)
        self.store(symbol, updated)
        return self.load(symbol)

    def preload(self, data):
        """Prepare data to serve to the user."""
        return data


class YfinanceBase(BaseData):
    """Base class for the Yahoo Finance interface.

    This should not be used directly unless you know what you're doing.
    """

    def internal_process(self, data):
        """Manipulate yfinance data for better storing."""
        intraday_logreturn = np.log(data["Close"]) - np.log(data["Open"])
        close_to_close_logreturn = np.log(data["Adj Close"]).diff().shift(-1)
        open_to_open_logreturn = (
            close_to_close_logreturn + intraday_logreturn - intraday_logreturn.shift(-1)
        )
        data["Return"] = np.exp(open_to_open_logreturn) - 1
        del data["Adj Close"]
        # eliminate intraday data
        data.loc[data.index[-1], ["High", "Low", "Close", "Return", "Volume"]] = np.nan
        return data

    def download(self, symbol, current=None, overlap=5, **kwargs):
        """Download single stock from Yahoo Finance.

        If data was already downloaded we only download
        the most recent missing portion.

        Args:

            symbol (str): yahoo name of the instrument
            current (pandas.DataFrame or None): current data present locally
            overlap (int): how many lines of current data will be overwritten
                by newly downloaded data
            kwargs (dict): extra arguments passed to yfinance.download

        Returns:
            updated (pandas.DataFrame): updated DataFrame for the symbol
        """
        if (current is None) or (len(current) < overlap):
            updated = yf.download(symbol, **kwargs)
            return self.internal_process(updated)
        else:
            new = yf.download(symbol, start=current.index[-overlap], **kwargs)
            new = self.internal_process(new)
            return pd.concat([current.iloc[:-overlap], new])

    def preload(self, data):
        """Prepare data for use by Cvxportfolio.

        We drop the 'Volume' column expressed in number of stocks
        and replace it with 'ValueVolume' which is an estimate
        of the (e.g., US dollar) value of the volume exchanged
        on the day.
        """
        data["ValueVolume"] = data["Volume"] * data["Open"]
        del data["Volume"]
        return data

class SqliteDataStore(BaseData):
    """Local sqlite3 database using python standard library.
    
    Args:
        location (pathlib.Path or None): pathlib.Path base location of the databases 
            directory or, if None, use ":memory:" for storing in RAM instead. Default
            is ~/cvxportfolio/
    """
    
    def __init__(self, base_location=Path.home() / "cvxportfolio"):
        """Initialize sqlite connection and if necessary create database."""
        if base_location is None:
             self.connection = sqlite3.connect(":memory:")
        else:
            self.connection = sqlite3.connect((base_location / self.__class__.__name__).with_suffix('.sqlite'))
        
    def store(self, symbol, data):
        """Store Pandas object to sqlite.
        
        We separately store dtypes for data consistency and safety.
        """
        exists = pd.read_sql_query(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{symbol}'", self.connection)
        if len(exists):
            res = self.connection.cursor().execute(f"DROP TABLE '{symbol}'")
            res = self.connection.cursor().execute(f"DROP TABLE '{symbol}___dtypes'")
            self.connection.commit()
            
        data.to_sql(f'{symbol}', self.connection)
        pd.DataFrame(data).dtypes.astype("string").to_sql(f'{symbol}___dtypes', self.connection)
                
    def load_raw(self, symbol):
        """Load Pandas object with datetime index from sqlite.
        
        If data is not present in in the database, return None.
        """
        try:
            dtypes = pd.read_sql_query(f"SELECT * FROM {symbol}___dtypes", self.connection, index_col='index', dtype={'index':'str', '0':'str'})
            tmp = pd.read_sql_query(f"SELECT * FROM {symbol}", self.connection, index_col='index', parse_dates='index', dtype=dict(dtypes['0']))
            return tmp.iloc[:, 0] if tmp.shape[1] == 1 else tmp
        except pd.errors.DatabaseError:
            return None
    
    

class LocalDataStore(BaseData):
    """Local data store for pandas Series and DataFrames.

    Args:
        base_location (pathlib.Path): filesystem directory where to store files.

    """

    def __init__(self, base_location=Path.home() / "cvxportfolio"):
        self.location = base_location / self.__class__.__name__
        if not self.location.is_dir():
            self.location.mkdir(parents=True)
            print(f"Created folder at {self.location}")

    def load_raw(self, symbol, **kwargs):
        """Load raw data from local store."""
        try:
            dtypes = pd.read_csv(self.location / f"{symbol}___dtypes.csv", index_col=0, dtype={'index':'str', '0':'str'})
            
            dtypes = dict(dtypes['0'])
            new_dtypes = {}
            parse_dates = [0]
            for i, el in enumerate(dtypes):
                if dtypes[el] == 'datetime64[ns]':
                    parse_dates += [i+1]
                else:
                    new_dtypes[el] = dtypes[el]
            tmp = pd.read_csv(
                self.location / f"{symbol}.csv", index_col=0, parse_dates=parse_dates, **kwargs, dtype=new_dtypes,
            )
            return tmp.iloc[:, 0] if tmp.shape[1] == 1 else tmp
        except FileNotFoundError:
            return None

    def store(self, symbol, data, **kwargs):
        """Store data locally."""
        data.to_csv(self.location / f"{symbol}.csv", **kwargs)
        pd.DataFrame(data).dtypes.astype("string").to_csv(self.location / f"{symbol}___dtypes.csv")


class FredBase(BaseData):
    """Base class for FRED data access."""

    def download(self, symbol="DFF", current=None):
        if current is None:
            end = pd.Timestamp.today()
            return pandas_datareader.get_data_fred(
                symbol, start="1900-01-01", end=pd.Timestamp.today()
            )[symbol]
        else:
            new = pandas_datareader.get_data_fred(
                symbol, start=current.index[-1], end=pd.Timestamp.today()
            )[symbol]
            assert new.index[0] == current.index[-1]
            return pd.concat([current.iloc[:-1], new])


class RateBase(BaseData):
    """Manipulate rate data from percent annualized to daily."""

    trading_days = 250

    def preload(self, data):
        return np.exp(np.log(1 + data / 100) / self.trading_days) - 1


class Yfinance(YfinanceBase, LocalDataStore):

    """Yahoo Finance data interface using local data store.

    Args:
        base_location (pathlib.Path): filesystem directory where to store files.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def update_and_load(self, symbol):
        """Update data for symbol and load it."""
        return super().update_and_load(symbol)


class FredRate(FredBase, RateBase, LocalDataStore):
    """Load and store FRED rates like DFF."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def update_and_load(self, symbol):
        """Update data for symbol and load it."""
        return super().update_and_load(symbol)
        
        
class DataError(Exception):
    """Base class for exception related to data."""
    pass
    
class MissingValuesError(DataError):
    """Cvxportfolio tried to access numpy.nan values."""
    pass
