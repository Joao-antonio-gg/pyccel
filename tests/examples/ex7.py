# coding: utf-8

from numpy import zeros

a = zeros(shape=(10,10), dtype=float)

for i in range(0,10):
    for j in range(0,10):
        a[i,j] = (i - j)*1.0

for i in range(0,10):
    a[i,i] = 2.0

#for i in range(0,9):
#    a[i,i+1] = -1.0
#    a[i+1,i] = -1.0
