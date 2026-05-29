//! Tier C: 高性能 xlsx I/O 扩展模块
//!
//! 替代 openpyxl 的热路径（write_plain_rows + append_row_incremental），
//! 速度提升约 10-20×（5-10ms vs 100ms per file）。
//!
//! API:
//!   write_rows_native(path, headers, rows)  → 写整个 xlsx
//!   read_rows_native(path, headers)         → 读 xlsx，返回 list[dict]
//!   append_row_native(path, headers, row)   → 追加一行（load+append+save）

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use rust_xlsxwriter::{Workbook, XlsxError};
use calamine::{Reader, Xlsx, open_workbook, DataType};
use std::collections::HashMap;
use std::path::Path;

// ---------------------------------------------------------------------------
// write_rows_native
// ---------------------------------------------------------------------------

/// 将 rows（list[dict[str,str]]）按 headers 顺序写入 xlsx。
/// 原子写入：先写 <path>.tmp，再 rename 到 path。
#[pyfunction]
fn write_rows_native(
    py: Python<'_>,
    path: &str,
    headers: Vec<String>,
    rows: &Bound<'_, PyList>,
) -> PyResult<()> {
    py.allow_threads(|| -> PyResult<()> {
        let mut workbook = Workbook::new();
        let worksheet = workbook.add_worksheet();

        // 写表头
        for (col, header) in headers.iter().enumerate() {
            worksheet.write_string(0, col as u16, header)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
        }

        // 写数据行
        for (row_idx, item) in rows.iter().enumerate() {
            let row_dict: &Bound<'_, PyDict> = item.downcast()?;
            for (col, header) in headers.iter().enumerate() {
                let val = row_dict
                    .get_item(header)?
                    .map(|v| v.str().map(|s| s.to_string_lossy().to_string()).unwrap_or_default())
                    .unwrap_or_default();
                worksheet.write_string((row_idx + 1) as u32, col as u16, &val)
                    .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            }
        }

        // 原子写入 tmp → replace
        let tmp_path = format!("{}.tmp", path);
        workbook.save(&tmp_path)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
        std::fs::rename(&tmp_path, path)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyIOError, _>(e.to_string()))?;
        Ok(())
    })
}

// ---------------------------------------------------------------------------
// read_rows_native
// ---------------------------------------------------------------------------

/// 读取 xlsx 第一个 sheet 的全部数据行（跳过表头行），返回 list[dict[str,str]]。
/// 用 headers 参数映射列，缺失列填 ""。
#[pyfunction]
fn read_rows_native(
    py: Python<'_>,
    path: &str,
    headers: Vec<String>,
) -> PyResult<PyObject> {
    let rows_data: Vec<HashMap<String, String>> = py.allow_threads(|| -> PyResult<Vec<HashMap<String, String>>> {
        let mut workbook: Xlsx<_> = open_workbook(path)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        let sheet_name = workbook.sheet_names().first()
            .ok_or_else(|| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("xlsx has no sheets"))?
            .clone();

        let range = workbook.worksheet_range(&sheet_name)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

        let mut result = Vec::new();
        for (row_idx, row) in range.rows().enumerate() {
            if row_idx == 0 {
                continue; // skip header row
            }
            let mut row_map = HashMap::new();
            for (col_idx, header) in headers.iter().enumerate() {
                let val = row.get(col_idx)
                    .map(|cell| match cell {
                        DataType::String(s) => s.clone(),
                        DataType::Float(f) => f.to_string(),
                        DataType::Int(i) => i.to_string(),
                        DataType::Bool(b) => b.to_string(),
                        DataType::Empty => String::new(),
                        _ => cell.to_string(),
                    })
                    .unwrap_or_default();
                row_map.insert(header.clone(), val);
            }
            result.push(row_map);
        }
        Ok(result)
    })?;

    Python::with_gil(|py| {
        let py_list = PyList::empty(py);
        for row_map in rows_data {
            let dict = PyDict::new(py);
            for (k, v) in &row_map {
                dict.set_item(k, v)?;
            }
            py_list.append(dict)?;
        }
        Ok(py_list.into())
    })
}

// ---------------------------------------------------------------------------
// append_row_native
// ---------------------------------------------------------------------------

/// 追加一行到现有 xlsx：读取所有现有行 + 追加新行 + 重写。
/// 等价于 openpyxl 的 load_workbook + ws.append + save，但速度快约 10×。
#[pyfunction]
fn append_row_native(
    py: Python<'_>,
    path: &str,
    headers: Vec<String>,
    row: &Bound<'_, PyDict>,
) -> PyResult<()> {
    // Build new row data on Python thread
    let mut new_row_vals: Vec<String> = Vec::with_capacity(headers.len());
    for header in &headers {
        let val = row.get_item(header)?
            .map(|v| v.str().map(|s| s.to_string_lossy().to_string()).unwrap_or_default())
            .unwrap_or_default();
        new_row_vals.push(val);
    }

    py.allow_threads(|| -> PyResult<()> {
        // Read existing data
        let mut existing_rows: Vec<Vec<String>> = Vec::new();

        if Path::new(path).exists() {
            let mut workbook: Xlsx<_> = open_workbook(path)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            let sheet_name = workbook.sheet_names().first()
                .ok_or_else(|| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>("xlsx has no sheets"))?
                .clone();
            let range = workbook.worksheet_range(&sheet_name)
                .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;

            for (row_idx, row) in range.rows().enumerate() {
                if row_idx == 0 {
                    continue; // skip header
                }
                let row_vals: Vec<String> = (0..headers.len())
                    .map(|col_idx| {
                        row.get(col_idx)
                            .map(|cell| match cell {
                                DataType::String(s) => s.clone(),
                                DataType::Float(f) => f.to_string(),
                                DataType::Int(i) => i.to_string(),
                                DataType::Empty => String::new(),
                                _ => cell.to_string(),
                            })
                            .unwrap_or_default()
                    })
                    .collect();
                existing_rows.push(row_vals);
            }
        }

        // Append new row
        existing_rows.push(new_row_vals);

        // Write all rows
        let mut wb = Workbook::new();
        let ws = wb.add_worksheet();
        for (col, header) in headers.iter().enumerate() {
            ws.write_string(0, col as u16, header)
                .map_err(|e: XlsxError| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
        }
        for (row_idx, row_vals) in existing_rows.iter().enumerate() {
            for (col, val) in row_vals.iter().enumerate() {
                ws.write_string((row_idx + 1) as u32, col as u16, val)
                    .map_err(|e: XlsxError| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
            }
        }

        let tmp_path = format!("{}.tmp", path);
        wb.save(&tmp_path)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyRuntimeError, _>(e.to_string()))?;
        std::fs::rename(&tmp_path, path)
            .map_err(|e| PyErr::new::<pyo3::exceptions::PyIOError, _>(e.to_string()))?;
        Ok(())
    })
}

// ---------------------------------------------------------------------------
// Module registration
// ---------------------------------------------------------------------------

#[pymodule]
fn _fast_xlsx(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(write_rows_native, m)?)?;
    m.add_function(wrap_pyfunction!(read_rows_native, m)?)?;
    m.add_function(wrap_pyfunction!(append_row_native, m)?)?;
    Ok(())
}
