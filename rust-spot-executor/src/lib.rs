use pyo3::prelude::*;

pub mod spot_executor;
pub mod tycho_client;

use spot_executor::SpotExecutor;

#[pymodule]
fn rust_spot_executor(_py: Python, m: &PyModule) -> PyResult<()> {
    m.add_class::<SpotExecutor>()?;
    Ok(())
}