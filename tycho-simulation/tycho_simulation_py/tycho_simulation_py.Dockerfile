FROM quay.io/pypa/manylinux2014_x86_64

# openssl-sys crate requires this.
# See https://docs.rs/openssl/latest/openssl/#automatic
RUN yum install -y pkgconfig openssl-devel && yum clean all

RUN curl --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain=stable -y

ENV PATH="/root/.cargo/bin:$PATH"

RUN /opt/python/cp39-cp39/bin/python -m pip install maturin

WORKDIR /tycho_simulation/tycho_simulation_py
CMD /opt/python/cp39-cp39/bin/python -m maturin build --release --compatibility manylinux2014 -i /opt/python/cp39-cp39/bin/python