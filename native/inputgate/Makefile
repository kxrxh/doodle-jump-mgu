CXX ?= c++

mgu-inputgate.so: src/mgu-inputgate.cpp
	$(CXX) -std=c++14 -O2 -g0 -Wall -shared -fPIC -Isrc/vsomeip-include/interface/compat -o $@ $< -ldl
