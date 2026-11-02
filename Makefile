VV := ./veriview.sh -q

.PHONY: help test lint examples clean

help:
	@echo "make test      - run the test suite"
	@echo "make lint      - run shellcheck on the scripts"
	@echo "make examples  - regenerate results/*.svg from ex_design/"

test:
	tests/run_tests.sh

lint:
	shellcheck veriview.sh tests/run_tests.sh

examples:
	$(VV) ex_design/counter.sv          -o results/counter.svg
	$(VV) ex_design/shift_reg.sv        -o results/shift_reg.svg
	$(VV) ex_design/seqdect.sv          -o results/seqdect.svg
	$(VV) ex_design/mux/mux_basic.sv    -o results/mux_basic.svg
	$(VV) ex_design/rca/rca.f           -o results/rca.svg
	$(VV) ex_design/rca/rca.f fa        -o results/fa.svg
	$(VV) ex_design/rca/rca.f --flatten -o results/rca_flat.svg
	$(VV) ex_design/rca/rca.f fa --flatten -o results/fa_flat.svg
	$(VV) ex_design/rca/ha.sv           -o results/ha.svg
	$(VV) ex_design/gray/gray.f         -o results/b2g2b.svg
	$(VV) ex_design/gray/gray.f --flatten -o results/b2g2b_flat.svg
	-$(VV) ex_design/mux/mux.sv         -o results/mux.svg

clean:
	rm -f ./*.svg ./*.json
