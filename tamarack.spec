# SPDX-License-Identifier: GPL-3.0-or-later
#
# Packaged per the Fedora Python Packaging Guidelines (pyproject-rpm-macros).
# https://docs.fedoraproject.org/en-US/packaging-guidelines/Python/

Name:           tamarack
Version:        0.1.0
Release:        1%{?dist}
Summary:        Self-healing storage planner for mixed-size drives

License:        GPL-3.0-or-later
URL:            https://github.com/theronconrey/tamarack
Source:         %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
# Generates the man page from the argparse definition.
BuildRequires:  /usr/bin/argparse-manpage

# Runtime tools (mdadm, lvm2, cryptsetup, btrfs-progs, gdisk) are not required
# yet: this release only plans layouts and prints dry-run commands. They become
# Requires when the storage engine starts executing changes.

%description
Tamarack plans Drobo-style storage for mixed-size drives on Fedora, built from
stock tools: dm-integrity, mdadm, LVM and Btrfs. It picks the redundancy
level automatically from the drives present, lays drives out in SHR-style
layers, and plans adding drives one at a time.

This release is planning only: it prints layouts, layout files and dry-run
command sequences, and never writes to a disk.

%prep
%autosetup -p1 -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires -x test

%build
%pyproject_wheel
PYTHONPATH=src argparse-manpage --module tamarack.cli --function build_parser \
    --project-name tamarack --prog tamarack --url %{url} --output tamarack.1

%install
%pyproject_install
%pyproject_save_files -l tamarack
install -Dpm 0644 tamarack.1 %{buildroot}%{_mandir}/man1/tamarack.1

%check
%pyproject_check_import
# Test the installed package, not the source tree the dev config points at.
%pytest -o pythonpath=

%files -f %{pyproject_files}
%doc README.md CONTRIBUTING.md
%{_bindir}/tamarack
%{_mandir}/man1/tamarack.1*

%changelog
* Sun Nov 22 2026 Theron Conrey <theron@conrey.org> - 0.1.0-1
- Initial package
