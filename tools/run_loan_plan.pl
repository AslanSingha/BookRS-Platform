# Execute a loan plan through Koha's own circulation, so that what the
# harvester later reads from /api/v1/checkouts is real.
# Usage (inside the koha container): perl run_loan_plan.pl plan.tsv
use strict;
use warnings;
use lib '/kohadevbox/koha/t/lib';
use lib '/kohadevbox/koha';
use C4::Context;
use C4::Circulation qw( AddIssue AddReturn AddRenewal );
use Koha::Items;
use Koha::Patrons;
use Koha::Patron;
use Koha::Patron::Categories;
use Koha::Libraries;
use t::lib::Mocks;

my $plan = shift or die "usage: run_loan_plan.pl plan.tsv\n";
my $lib   = Koha::Libraries->search({}, { rows => 1 })->next;
my $staff = Koha::Patrons->search({}, { rows => 1 })->next;
t::lib::Mocks::mock_userenv({ patron => $staff, branchcode => $lib->branchcode });
my $cat = Koha::Patron::Categories->search({ category_type => 'A' }, { rows => 1 })->next
       // Koha::Patron::Categories->search({}, { rows => 1 })->next;

open my $fh, '<', $plan or die "cannot open $plan: $!";
my @rows = map { chomp; [ split /\t/ ] } <$fh>;
close $fh;
my $n_patrons = 1 + (sort { $b <=> $a } map { $_->[0] } @rows)[0];

# synthetic patrons, created once, found again on re-runs
my @patrons;
for my $i (0 .. $n_patrons - 1) {
    my $card = sprintf('SYN%05d', $i);
    my $p = Koha::Patrons->find({ cardnumber => $card });
    $p //= Koha::Patron->new({
        cardnumber => $card, surname => 'Synthetic', firstname => sprintf('Patron %d', $i),
        categorycode => $cat->categorycode, branchcode => $lib->branchcode,
        dateenrolled => '2024-01-01', dateexpiry => '2030-12-31',
    })->store;
    push @patrons, $p;
}
printf "    %d patrons ready (category %s, branch %s)\n", scalar @patrons, $cat->categorycode, $lib->branchcode;

my ($issued, $renewed, $returned, $repeats, $noitem, $failed) = (0) x 6;
my $t0 = time;
for my $r (@rows) {
    my ($pi, $bib, $renew, $ret, $rep) = @$r;
    my $patron = $patrons[$pi] or next;
    my $item = Koha::Items->search({ biblionumber => $bib, onloan => undef, notforloan => 0 }, { rows => 1 })->next;
    unless ($item && $item->barcode) { $noitem++; next }
    my $issue = eval { AddIssue($patron, $item->barcode, undef, undef, undef, undef, {}) };
    unless ($issue) { $failed++; next }
    $issued++;
    if ($renew) {
        eval { AddRenewal({ borrowernumber => $patron->borrowernumber, itemnumber => $item->itemnumber, branch => $lib->branchcode }) };
        $renewed++;
    }
    if ($ret) {
        eval { AddReturn($item->barcode, $lib->branchcode) }; $returned++;
        if ($rep) {
            my $again = eval { AddIssue($patron, $item->barcode, undef, undef, undef, undef, {}) };
            if ($again) { $repeats++; eval { AddReturn($item->barcode, $lib->branchcode) }; $returned++; }
        }
    }
    printf "    %d issued...\n", $issued if $issued % 1000 == 0;
}
printf "    issued %d, renewed %d, returned %d, repeat borrows %d, no lendable item %d, refused %d, %ds\n",
    $issued, $renewed, $returned, $repeats, $noitem, $failed, time - $t0;
